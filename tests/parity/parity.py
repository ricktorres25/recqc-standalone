#!/usr/bin/env python3
# parity.py - the parity test (DESIGN_NOTE.md section 3; M1 gate 1, 2026-10-21; M2 adds
# the 44.1 kHz set; M3 adds the page's flow and P3; M4 adds room reflection and the
# summary to the page's flow).
#
# Do the vendored cores score the Testing corpus the same in Pyodide as in CPython,
# and does CPython still reproduce the corpus's stored records?
#
#   P1  Pyodide (in Node) against CPython, every field of every record, with the
#       tolerances in compare.py, for three sets: the corpus under each profile (the
#       cores' defaults and Platform Band), and the corpus resampled to 44.1 kHz under
#       Platform Band. The 44.1 kHz copies are made once here with scipy (not soxr), so
#       both sides start from the same files and each derives its 48 kHz working copies
#       with its own soxr (0.5.0.post1 in Pyodide, 1.1.0 live). The largest sample
#       difference between the two sides' working copies is reported too.
#       Five more sets run the page's flow (run_page_corpus: the level take, the
#       room-tone take only if the level gate passed, the clap take only if the
#       quiet-room gate passed outside the lo-fi band, then the ceiling and margins)
#       under Platform Band (the profile the page offers, as Default band), the noise
#       core's Default (with the RT60 core's built-in, Strict), a user's yaml (the
#       fixture in this folder), and Platform Band at 44.1 kHz; and once with Forgiving
#       for room reflection. The page offers only Platform Band and the user's yaml; the
#       other profiles stay covered because the runner still accepts them.
#   P2  CPython against the stored records in each folder's screening_result.json,
#       defaults only (the June records used them), same tolerances. One difference is
#       expected and listed, not counted: the live RT60 core is newer than the one that
#       wrote the records and adds "dry_room": null.
#   P3  In CPython, the page's flow against run_folder on the same takes and profile:
#       the level, quiet-room and room-reflection records must agree; the page must stop
#       exactly where the level gate or the quiet-room gate fails, and skip room
#       reflection exactly where run_folder's quiet room reads WARN (the lo-fi band). The
#       identity fields differ by design and are listed: the page has no talent or
#       language to pass.
#   N   Near misses (builds:grader-negative-control): the comparison must catch every
#       folder's record paired with the wrong folder's, a value nudged just past each
#       tolerance and a changed verdict, and must pass a value nudged just inside each
#       tolerance. A near miss that goes uncaught fails the run.
#   Also: CPython must carry the live platform's library versions (refused otherwise,
#   exit 2), every pyodide/ file must match pyodide/VENDORED.json before Node starts
#   (Node checks none), and pyodide/ must hold no new file afterwards (a missing wheel
#   would have been downloaded into it).
#
# Read-only on the corpus: the takes are copied into a work folder per side and
# profile, the cores write there, and the folder is removed at the end (--keep keeps it).
#
# CI (M5) runs it on a synthetic corpus (tests/parity/synth_corpus.py) with --no-stored:
# P1, P3 and N as above, P2 skipped, since a synthetic take has no stored record. The
# real corpus run, P2 included, stays the release check on Rick's Mac.
#
# Run:   python3 tests/parity/parity.py ["<Testing corpus>"] [--no-stored] [--keep] [--report out.json]
#        (pyenv 3.12.7 on Rick's Mac carries the live library versions)
# Exit:  0 pass, 1 a difference, 2 not run (an input or tool was missing; never a pass)
import sys
sys.dont_write_bytecode = True   # leave no __pycache__ in py/

import argparse
import copy
import hashlib
import json
import shutil
import subprocess
import tempfile
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "py"))
sys.path.insert(0, str(HERE))
import compare  # noqa: E402

DEFAULT_CORPUS = REPO.parent / "Screening Pipeline" / "Testing corpus"
PROFILES = ("default", "platform_band")

# P1's sets: name -> (profile, capture rate). A 44.1 kHz set holds every corpus take
# resampled from 48 kHz by 147/160.
SETS = {"default": ("default", 48000),
        "platform_band": ("platform_band", 48000),
        "platform_band_44k": ("platform_band", 44100)}
# P1's page sets (M3, M4): name -> (quiet-room profile, room-reflection profile, capture
# rate), scored the page's way. FIXTURE stands for the user's yaml; each side reads its
# own copy of it. page_default pairs the noise core's built-in with the RT60 core's
# (Strict), as run_folder's default set does.
FIXTURE = "fixture_noise_profile.yaml"
PAGE_SETS = {"page_platform_band": ("platform_band", "platform_band", 48000),
             "page_default": ("default", "strict", 48000),
             "page_yours": (FIXTURE, "platform_band", 48000),
             "page_platform_band_44k": ("platform_band", "platform_band", 44100),
             "page_forgiving": ("platform_band", "forgiving", 48000)}
# P3: each page set against the run_folder set scored the same way.
PAGE_PAIRS = {"page_platform_band": "platform_band", "page_default": "default",
              "page_platform_band_44k": "platform_band_44k"}
STAGES = {"level": "level_check", "noise": "noise_profile", "rt60": "rt60"}
GATES = {"level_check": "level_gate", "noise_profile": "noise_gate", "rt60": "rt60_gate"}

# The live platform's library versions (its requirements.txt), read on the box 2026-10-21.
LIVE_VERSIONS = {"numpy": "2.4.4", "scipy": "1.16.3", "soundfile": "0.13.1", "soxr": "1.1.0",
                 "PyYAML": "6.0.3", "pyloudnorm": "0.1.1", "future": "1.0.0"}


def not_run(message):
    print(f"NOT RUN  {message}")
    sys.exit(2)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dry_room_added(path, a, b):
    """P2's one expected difference: RT60 records from the live core carry dry_room: null."""
    if len(path) >= 2 and path[1] == "rt60" and path[-1] == "dry_room" \
            and a is None and b == compare.MISSING:
        return "RT60 dry_room: null (the live core is newer than the June records)"
    return None


def identity_only(path, a, b):
    """P3's expected difference: run_folder passes the talent and language it reads from
    the file name, and the page has none to pass, so the core stores ""."""
    if str(path[-1]) in ("talent_id", "language_code") and a == "" and b:
        return "talent_id and language_code: run_folder reads them from the file name; the page has none"
    return None


def get_path(record, path):
    """The value at a key path, or None when any step is missing."""
    node = record
    for p in path:
        try:
            node = node[p]
        except (KeyError, IndexError, TypeError):
            return None
    return node


def near_misses(a_rows, b_rows):
    """The negative controls (N). Prints one line each; returns True when all were caught."""
    ok = True

    def check(caught, text):
        nonlocal ok
        ok &= caught
        print(f"{'PASS' if caught else 'FAIL':8s} N {text}")

    # Every folder's record against the next folder's must differ.
    shifted = b_rows[1:] + b_rows[:1]
    missed = 0
    for a, b in zip(a_rows, shifted):
        comp = compare.Comparison()
        comp.walk(a, b, (a["folder"],))
        missed += not comp.differences
    check(missed == 0, f"wrong-folder pairings caught: {len(a_rows) - missed} of {len(a_rows)}")

    # Tolerance edges, on the first real record that has all three fields.
    edges = [(("level", "true_peak_dbtp"), compare.TOL_DB, "dB"),
             (("rt60", "summary", "rt60_gate_metric"), compare.TOL_SECONDS, "RT60 s"),
             (("rt60", "claps", 0, "fit_quality", "1000", "r_squared"), compare.TOL_R_SQUARED, "r2")]
    record = next((r for r in b_rows
                   if all(isinstance(get_path(r, p), float) for p, _, _ in edges)), None)
    if record is None:
        check(False, "tolerance edges: no record has all three fields, so none was tried")
        return ok
    for path, tol, name in edges:
        for factor, must_differ in ((1.1, True), (0.9, False)):
            nudged = copy.deepcopy(record)
            get_path(nudged, path[:-1])[path[-1]] += tol * factor
            comp = compare.Comparison()
            comp.walk(nudged, record, (record["folder"],))
            check(bool(comp.differences) == must_differ,
                  f"{name} nudged by {factor:g} x its tolerance "
                  f"{'caught' if must_differ else 'let through'}")

    nudged = copy.deepcopy(record)
    nudged["level_gate"] = ["FAIL", nudged["level_gate"][1]]
    comp = compare.Comparison()
    comp.walk(nudged, record, (record["folder"],))
    check(bool(comp.differences), "a changed gate verdict caught")
    return ok


def summary_near_misses(rows):
    """The negative controls for the page's summary (M4): a metric row's value and a
    margin take the tolerance of the unit beside them, and a changed ceiling is caught."""
    ok = True

    def check(caught, text):
        nonlocal ok
        ok &= caught
        print(f"{'PASS' if caught else 'FAIL':8s} N {text}")

    def metric(record, name):
        return next((i for i, m in enumerate(record["summary"]["metrics"]) if m["metric"] == name), None)

    record = next((r for r in rows if metric(r, "RT60 at 1 kHz (T20)") is not None
                   and isinstance(r["summary"]["metrics"][metric(r, "RT60 at 1 kHz (T20)")]["value"], float)), None)
    if record is None:
        check(False, "summary edges: no page record has an RT60 row, so none was tried")
        return ok
    snr, rt = metric(record, "SNR (A-weighted)"), metric(record, "RT60 at 1 kHz (T20)")
    edges = [(("summary", "metrics", snr, "value"), compare.TOL_DB, "summary SNR value (dB)"),
             (("summary", "metrics", snr, "margins", 0, "by"), compare.TOL_DB, "summary SNR margin (dB)"),
             (("summary", "metrics", rt, "value"), compare.TOL_SECONDS, "summary RT60 value (s)")]
    for path, tol, name in edges:
        for factor, must_differ in ((1.1, True), (0.9, False)):
            nudged = copy.deepcopy(record)
            get_path(nudged, path[:-1])[path[-1]] += tol * factor
            comp = compare.Comparison()
            comp.walk(nudged, record, (record["folder"],))
            check(bool(comp.differences) == must_differ,
                  f"{name} nudged by {factor:g} x its tolerance {'caught' if must_differ else 'let through'}")
    nudged = copy.deepcopy(record)
    nudged["summary"]["ceiling"]["ceiling"] = 1 + (nudged["summary"]["ceiling"]["ceiling"] or 0) % 4
    comp = compare.Comparison()
    comp.walk(nudged, record, (record["folder"],))
    check(bool(comp.differences), "a changed ceiling caught")
    return ok


def write_44k(take, dest):
    """Write a 44.1 kHz, 24-bit copy of a 48 kHz take, clipped to the 24-bit range (the
    filter can overshoot full scale on a hot take, and a PCM write would wrap it)."""
    x, rate = sf.read(str(take), dtype="float64")
    if rate != 48000:
        raise ValueError(f"{take.name} is {rate} Hz; the 44.1 kHz set is made from 48 kHz takes")
    y = resample_poly(x, 147, 160)
    sf.write(str(dest), np.clip(y, -1.0, 1.0 - 2.0 ** -23), 44100, subtype="PCM_24")


def working_copy_difference(work, name):
    """Compare the two sides' derived working copies in one set, file by file.

    Returns (largest sample difference in 24-bit steps, files compared, problems): a
    working copy only one side made, or two of different lengths, is a problem.
    """
    worst, compared, problems = 0.0, 0, []
    a_dir, b_dir = work / "cpython" / name, work / "pyodide" / name
    a_files = {p.relative_to(a_dir) for p in a_dir.rglob("*_48k.wav")}
    b_files = {p.relative_to(b_dir) for p in b_dir.rglob("*_48k.wav")}
    problems += [f"only CPython made {p}" for p in sorted(a_files - b_files)]
    problems += [f"only Pyodide made {p}" for p in sorted(b_files - a_files)]
    for rel in sorted(a_files & b_files):
        a, _ = sf.read(str(a_dir / rel), dtype="float64")
        b, _ = sf.read(str(b_dir / rel), dtype="float64")
        if len(a) != len(b):
            problems.append(f"{rel}: {len(a)} samples (CPython) vs {len(b)} (Pyodide)")
            continue
        worst = max(worst, float(np.max(np.abs(a - b))) * 2 ** 23)
        compared += 1
    return worst, compared, problems


def show(comp, title, limit=25):
    """Print one comparison's result; return True when it passed."""
    ok = not comp.differences
    print(f"{'PASS' if ok else 'FAIL':8s} {title}: {comp.values} values, "
          f"{len(comp.differences)} differences, {len(comp.explained)} expected")
    for kind, (label, delta) in sorted(comp.largest_by_kind().items()):
        print(f"         largest {kind:3s} delta {delta:.3g}  ({label})")
    for path, a, b, why in comp.differences[:limit]:
        print(f"         DIFF {'.'.join(map(str, path))}: {a!r} vs {b!r} ({why})")
    if len(comp.differences) > limit:
        print(f"         ... and {len(comp.differences) - limit} more")
    reasons = sorted({reason for _, reason in comp.explained})
    for reason in reasons:
        print(f"         expected: {reason}")
    return ok


def main():
    ap = argparse.ArgumentParser(description="Parity: Pyodide vs CPython vs stored records")
    ap.add_argument("corpus", nargs="?", default=str(DEFAULT_CORPUS))
    ap.add_argument("--keep", action="store_true", help="keep the work folder")
    ap.add_argument("--no-stored", action="store_true",
                    help="skip P2 (a corpus without stored records, such as the synthetic one in CI)")
    ap.add_argument("--report", help="write the full comparison as JSON here")
    args = ap.parse_args()

    corpus = Path(args.corpus)
    if not corpus.is_dir():
        not_run(f"corpus not found: {corpus}")
    node = shutil.which("node")
    if not node:
        not_run("node not found on PATH")

    # 1. CPython must be the live platform's library set.
    found = {}
    for name, pinned in LIVE_VERSIONS.items():
        try:
            found[name] = version(name)
        except PackageNotFoundError:
            found[name] = None
    drift = {n: (found[n], v) for n, v in LIVE_VERSIONS.items() if found[n] != v}
    if drift:
        not_run("CPython libraries differ from the live platform's: " + ", ".join(
            f"{n} {have} (live {want})" for n, (have, want) in drift.items()))
    print(f"CPython  {sys.version.split()[0]}  " +
          " ".join(f"{n} {v}" for n, v in found.items()) + "  (the live platform's versions)")

    # 2. Every vendored Pyodide file must match its manifest before Node loads it.
    manifest = json.loads((REPO / "pyodide" / "VENDORED.json").read_text(encoding="utf-8"))
    bad = [f["path"] for f in manifest["files"]
           if not (REPO / "pyodide" / f["path"]).is_file()
           or sha256(REPO / "pyodide" / f["path"]) != f["sha256"]]
    if bad:
        print(f"FAIL     pyodide/ files differ from pyodide/VENDORED.json: {', '.join(bad)}")
        return 1
    listing_before = sorted(p.name for p in (REPO / "pyodide").iterdir())
    print(f"Pyodide  {manifest['pyodide_version']}: {len(manifest['files'])} files match "
          "pyodide/VENDORED.json")

    import recqc_runner   # importing it runs the cores' start check

    # 3. Copy the takes: one folder per side and set, so no run sees another's outputs.
    #    A 44.1 kHz set gets resampled copies, written once and copied to the other side.
    work = Path(tempfile.mkdtemp(prefix="recqc-parity-"))
    folders = [d for d in sorted(corpus.iterdir())
               if d.is_dir() and any(d.glob("*" + recqc_runner.TAKE_SUFFIXES["phrase"]))]
    all_sets = {**SETS, **PAGE_SETS}
    for name, spec in all_sets.items():
        rate = spec[-1]
        for folder in folders:
            for side in ("cpython", "pyodide"):
                (work / side / name / folder.name).mkdir(parents=True)
            for take in recqc_runner.find_takes(folder).values():
                first = work / "cpython" / name / folder.name / take.name
                if rate == 48000:
                    shutil.copy2(take, first)
                else:
                    write_44k(take, first)
                shutil.copy2(first, work / "pyodide" / name / folder.name / take.name)
    for side in ("cpython", "pyodide"):
        shutil.copy2(HERE / FIXTURE, work / side / FIXTURE)
    # The user's yaml as each side names it (the Pyodide side sees the work folder at /work).
    yours = {"cpython": str(work / "cpython" / FIXTURE), "pyodide": f"/work/{FIXTURE}"}
    page_profile = lambda profile, side: yours[side] if profile == FIXTURE else profile  # noqa: E731
    print(f"Corpus   {corpus.name}: {len(folders)} folders, takes copied to {work} "
          f"({len(all_sets)} sets: {', '.join(all_sets)})")

    try:
        # 4. CPython side, in this process. A JSON round trip makes both sides' types match.
        cpython, cpython_pages, cpython_seconds = {}, {}, {}
        for name, (profile, _) in SETS.items():
            t0 = time.perf_counter()
            rows = recqc_runner.run_corpus(work / "cpython" / name, profile)
            cpython_seconds[name] = time.perf_counter() - t0
            cpython[name] = json.loads(json.dumps(rows))
        for name, (profile, rt60_profile, _) in PAGE_SETS.items():
            t0 = time.perf_counter()
            rows = recqc_runner.run_page_corpus(work / "cpython" / name, page_profile(profile, "cpython"),
                                                rt60_profile)
            cpython_seconds[name] = time.perf_counter() - t0
            cpython_pages[name] = json.loads(json.dumps(rows))

        # 5. Pyodide side, in Node.
        out_json = work / "pyodide.json"
        t0 = time.perf_counter()
        specs = [f"{n}:{p}" for n, (p, _) in SETS.items()] + \
                [f"page:{n}:{page_profile(p, 'pyodide')}:{r}" for n, (p, r, _) in PAGE_SETS.items()]
        proc = subprocess.run([node, str(HERE / "run_pyodide.mjs"), str(work / "pyodide"),
                               str(out_json), *specs],
                              capture_output=True, text=True, timeout=3600)
        node_seconds = time.perf_counter() - t0
        if proc.returncode != 0 or not out_json.is_file():
            print("FAIL     the Pyodide side did not finish:\n" + proc.stderr[-3000:])
            return 1
        pyo = json.loads(out_json.read_text(encoding="utf-8"))
        libs = dict(pyo["libraries"])
        print(f"Node     {pyo['node_version']}: Pyodide {pyo['pyodide_version']} (Python "
              f"{libs.pop('python')}) " + " ".join(f"{n} {v}" for n, v in libs.items()) +
              f"; booted in {pyo['boot_seconds']} s")
        print()

        # 6. P1: Pyodide against CPython, per set and folder.
        results, passed = {}, True
        for set_name in SETS:
            comp = compare.Comparison()
            a_rows = {r["folder"]: r for r in pyo["profiles"][set_name]}
            b_rows = {r["folder"]: r for r in cpython[set_name]}
            for name in sorted(set(a_rows) | set(b_rows)):
                comp.walk(a_rows.get(name, compare.MISSING), b_rows.get(name, compare.MISSING),
                          (name,))
            passed &= show(comp, f"P1 {set_name}: Pyodide (A) vs CPython (B)")
            results[f"P1 {set_name}"] = comp
        for set_name in PAGE_SETS:
            comp = compare.Comparison()
            a_rows = {r["folder"]: r for r in pyo["pages"][set_name]}
            b_rows = {r["folder"]: r for r in cpython_pages[set_name]}
            for name in sorted(set(a_rows) | set(b_rows)):
                comp.walk(a_rows.get(name, compare.MISSING), b_rows.get(name, compare.MISSING),
                          (name,))
            ran = sum("skipped" not in r["noise"] for r in cpython_pages[set_name])
            claps = sum("skipped" not in r["clap"] for r in cpython_pages[set_name])
            passed &= show(comp, f"P1 {set_name}: Pyodide (A) vs CPython (B), "
                                 f"quiet room on {ran}, room reflection on {claps} of {len(b_rows)} folders")
            results[f"P1 {set_name}"] = comp

        # The derived working copies themselves, for each resampled set.
        for set_name, spec in all_sets.items():
            if spec[-1] == 48000:
                continue
            worst, compared, problems = working_copy_difference(work, set_name)
            ok = not problems and compared > 0
            passed &= ok
            print(f"{'PASS' if ok else 'FAIL':8s} P1 {set_name} working copies: {compared} files, "
                  f"largest sample difference {worst:.0f} x 24-bit step "
                  f"(soxr {libs.get('soxr')} in Pyodide, {found['soxr']} in CPython)")
            for problem in problems:
                print(f"         {problem}")

        # 7. P2: CPython (defaults) against the stored records.
        comp = compare.Comparison(expected=[dry_room_added])
        for ours in ([] if args.no_stored else cpython["default"]):
            stored = json.loads((corpus / ours["folder"] / "screening_result.json")
                                .read_text(encoding="utf-8"))
            for key, stage in STAGES.items():
                if stored["stages"].get(stage) is not None:   # the pipeline may have stopped earlier
                    comp.walk(ours.get(key), stored["stages"][stage], (ours["folder"], key))
            for sr in stored["stage_results"]:
                comp.walk(ours.get(GATES[sr["stage"]]), [sr["verdict"], sr["reason"]],
                          (ours["folder"], GATES[sr["stage"]]))
        if args.no_stored:
            print("SKIPPED  P2 default: --no-stored (this corpus has no stored records; "
                  "the real corpus run covers P2)")
        else:
            passed &= show(comp, "P2 default: CPython (A) vs stored records (B)")
            results["P2 default"] = comp

        # 8. P3: the page's flow against run_folder, in CPython, on the same takes.
        for page_set, folder_set in PAGE_PAIRS.items():
            comp = compare.Comparison(expected=[identity_only])
            ref = {r["folder"]: r for r in cpython[folder_set]}
            ran = claps = 0
            for page in cpython_pages[page_set]:
                folder, theirs = page["folder"], ref[page["folder"]]
                comp.walk(page["level"].get("level"), theirs.get("level"), (folder, "level"))
                comp.walk(page["level"].get("level_gate"), theirs.get("level_gate"), (folder, "level_gate"))
                if "skipped" in page["noise"]:
                    # The page stops at a failed level gate; run_folder scores on regardless.
                    comp.walk("skipped", "skipped" if theirs["level_gate"][0] != "PASS" else "ran",
                              (folder, "quiet room"))
                    continue
                ran += 1
                comp.walk(page["noise"].get("noise"), theirs.get("noise"), (folder, "noise"))
                comp.walk(page["noise"].get("noise_gate"), theirs.get("noise_gate"), (folder, "noise_gate"))
                # The page stops at a failed quiet-room gate and skips room reflection in
                # the lo-fi band (noise WARN); run_folder scores the claps regardless.
                expect = ("skipped: gate" if theirs["noise_gate"][0] != "PASS"
                          else "skipped: lo-fi band" if theirs["noise"]["disposition"] == "WARN" else "ran")
                got = ("ran" if "skipped" not in page["clap"]
                       else "skipped: gate" if "gate" in page["clap"]["skipped"] else "skipped: lo-fi band")
                comp.walk(got, expect, (folder, "room reflection"))
                if got == expect == "ran":
                    claps += 1
                    comp.walk(page["clap"].get("rt60"), theirs.get("rt60"), (folder, "rt60"))
                    comp.walk(page["clap"].get("rt60_gate"), theirs.get("rt60_gate"), (folder, "rt60_gate"))
            passed &= show(comp, f"P3 {page_set}: the page's flow (A) vs run_folder {folder_set} (B), "
                                 f"quiet room on {ran}, room reflection on {claps} folders")
            results[f"P3 {page_set}"] = comp

        # N: the comparison has to be able to fail, or its passes above prove nothing.
        passed &= near_misses(pyo["profiles"]["default"], cpython["default"])
        passed &= summary_near_misses(cpython_pages["page_platform_band"])

        # 9. pyodide/ must be unchanged (Node would have downloaded a missing wheel into it).
        listing_after = sorted(p.name for p in (REPO / "pyodide").iterdir())
        if listing_after != listing_before:
            passed = False
            print("FAIL     pyodide/ changed during the run: "
                  + ", ".join(sorted(set(listing_after) ^ set(listing_before))))
        else:
            print("PASS     pyodide/ unchanged by the run (nothing was downloaded)")

        # 10. Timings, for the record (not compared).
        print()
        for set_name in SETS:
            for side, rows in (("CPython", cpython[set_name]), ("Pyodide", pyo["profiles"][set_name])):
                totals = {}
                for r in rows:
                    for stage, s in r["timings"].items():
                        totals[stage] = totals.get(stage, 0.0) + s
                print(f"Timing   {set_name:17s} {side}: " + ", ".join(
                    f"{stage} {s / len(rows):.2f} s" for stage, s in totals.items())
                    + " per folder")
        print(f"Timing   wall clock: CPython {sum(cpython_seconds.values()):.1f} s, "
              f"Node {node_seconds:.1f} s (boot {pyo['boot_seconds']} s)")

        if args.report:
            report = {name: {"values": c.values,
                             "differences": [[list(map(str, p)), repr(a), repr(b), why]
                                             for p, a, b, why in c.differences],
                             "expected": [[list(map(str, p)), r] for p, r in c.explained],
                             "largest": {k: list(v) for k, v in c.largest.items()}}
                      for name, c in results.items()}
            report["environment"] = {"cpython": found, "pyodide": pyo["libraries"],
                                     "pyodide_version": pyo["pyodide_version"],
                                     "node": pyo["node_version"]}
            Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(f"Report   {args.report}")
    finally:
        if args.keep:
            print(f"Kept     {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)

    print()
    print(("PARITY: PASS" + (" (P2 skipped: --no-stored)" if args.no_stored else "")) if passed
          else "PARITY: FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
