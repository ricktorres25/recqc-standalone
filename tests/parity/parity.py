#!/usr/bin/env python3
# parity.py - the M1 parity test (DESIGN_NOTE.md section 3; gate 1, 2026-10-21).
#
# Do the vendored cores score the Testing corpus the same in Pyodide as in CPython,
# and does CPython still reproduce the corpus's stored records?
#
#   P1  Pyodide (in Node) against CPython, for both profiles (the cores' defaults and
#       Platform Band), every field of every record, with the tolerances in compare.py.
#   P2  CPython against the stored records in each folder's screening_result.json,
#       defaults only (the June records used them), same tolerances. One difference is
#       expected and listed, not counted: the live RT60 core is newer than the one that
#       wrote the records and adds "dry_room": null.
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
# Run:   python3 tests/parity/parity.py ["<Testing corpus>"] [--keep] [--report out.json]
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

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "py"))
sys.path.insert(0, str(HERE))
import compare  # noqa: E402

DEFAULT_CORPUS = REPO.parent / "Screening Pipeline" / "Testing corpus"
PROFILES = ("default", "platform_band")
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
    ap = argparse.ArgumentParser(description="M1 parity: Pyodide vs CPython vs stored records")
    ap.add_argument("corpus", nargs="?", default=str(DEFAULT_CORPUS))
    ap.add_argument("--keep", action="store_true", help="keep the work folder")
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

    # 3. Copy the takes: one folder per side and profile, so no run sees another's outputs.
    work = Path(tempfile.mkdtemp(prefix="recqc-parity-"))
    folders = [d for d in sorted(corpus.iterdir())
               if d.is_dir() and any(d.glob("*" + recqc_runner.TAKE_SUFFIXES["phrase"]))]
    for side in ("cpython", "pyodide"):
        for profile in PROFILES:
            for folder in folders:
                dest = work / side / profile / folder.name
                dest.mkdir(parents=True)
                for take in recqc_runner.find_takes(folder).values():
                    shutil.copy2(take, dest / take.name)
    print(f"Corpus   {corpus.name}: {len(folders)} folders, takes copied to {work}")

    try:
        # 4. CPython side, in this process. A JSON round trip makes both sides' types match.
        cpython, cpython_seconds = {}, {}
        for profile in PROFILES:
            t0 = time.perf_counter()
            rows = recqc_runner.run_corpus(work / "cpython" / profile, profile)
            cpython_seconds[profile] = time.perf_counter() - t0
            cpython[profile] = json.loads(json.dumps(rows))

        # 5. Pyodide side, in Node.
        out_json = work / "pyodide.json"
        t0 = time.perf_counter()
        proc = subprocess.run([node, str(HERE / "run_pyodide.mjs"), str(work / "pyodide"),
                               str(out_json), *PROFILES],
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

        # 6. P1: Pyodide against CPython, per profile and folder.
        results, passed = {}, True
        for profile in PROFILES:
            comp = compare.Comparison()
            a_rows = {r["folder"]: r for r in pyo["profiles"][profile]}
            b_rows = {r["folder"]: r for r in cpython[profile]}
            for name in sorted(set(a_rows) | set(b_rows)):
                comp.walk(a_rows.get(name, compare.MISSING), b_rows.get(name, compare.MISSING),
                          (name,))
            passed &= show(comp, f"P1 {profile}: Pyodide (A) vs CPython (B)")
            results[f"P1 {profile}"] = comp

        # 7. P2: CPython (defaults) against the stored records.
        comp = compare.Comparison(expected=[dry_room_added])
        for ours in cpython["default"]:
            stored = json.loads((corpus / ours["folder"] / "screening_result.json")
                                .read_text(encoding="utf-8"))
            for key, stage in STAGES.items():
                if stored["stages"].get(stage) is not None:   # the pipeline may have stopped earlier
                    comp.walk(ours.get(key), stored["stages"][stage], (ours["folder"], key))
            for sr in stored["stage_results"]:
                comp.walk(ours.get(GATES[sr["stage"]]), [sr["verdict"], sr["reason"]],
                          (ours["folder"], GATES[sr["stage"]]))
        passed &= show(comp, "P2 default: CPython (A) vs stored records (B)")
        results["P2 default"] = comp

        # N: the comparison has to be able to fail, or its passes above prove nothing.
        passed &= near_misses(pyo["profiles"]["default"], cpython["default"])

        # 8. pyodide/ must be unchanged (Node would have downloaded a missing wheel into it).
        listing_after = sorted(p.name for p in (REPO / "pyodide").iterdir())
        if listing_after != listing_before:
            passed = False
            print("FAIL     pyodide/ changed during the run: "
                  + ", ".join(sorted(set(listing_after) ^ set(listing_before))))
        else:
            print("PASS     pyodide/ unchanged by the run (nothing was downloaded)")

        # 9. Timings, for the record (not compared).
        print()
        for profile in PROFILES:
            for side, rows in (("CPython", cpython[profile]), ("Pyodide", pyo["profiles"][profile])):
                totals = {}
                for r in rows:
                    for stage, s in r["timings"].items():
                        totals[stage] = totals.get(stage, 0.0) + s
                print(f"Timing   {profile:13s} {side}: " + ", ".join(
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
    print("M1 PARITY: PASS" if passed else "M1 PARITY: FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
