#!/usr/bin/env python3
# test_vendored.py - the vendored files under py/cores/ stay honest.
#
#   V1  every file listed in py/cores/VENDORED.json matches its recorded sha256
#   V2  importing the cores package refuses to start on a mismatch, and names the file
#   V3  each vendored file is the committed file at the recorded commit in its source
#       repo; a lifted file is exactly what the lift renders from the source at that commit
#   V4  each lifted JavaScript file in app/VENDORED.json matches its sha256, is exactly
#       what the lift renders from its source at the recorded commit, and parses (node)
#
# Every check also runs its near miss (builds:grader-negative-control): a case that
# differs from a passing one only in the property being checked. A near miss that is
# not caught is a failure.
#
# V3 and V4 need the MAD source repos next to this repo (Rick's machine), and V4's parse
# check needs node; without them a check is reported NOT RUN, never PASS. Writes only
# inside a temporary folder; the source repos are only read (git with --no-optional-locks).
#
# Run:   python3 tests/test_vendored.py
# Exit:  0 pass, 1 fail, 2 not run (an input was missing; never a pass)
import sys
sys.dont_write_bytecode = True   # leave no __pycache__ in py/

import importlib.util
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORES = REPO / "py" / "cores"
APP = REPO / "app"
MAD = REPO.parent

# Load the check module by path, so a damaged copy cannot stop the test from starting.
_spec = importlib.util.spec_from_file_location("_vendor_check", CORES / "_vendor_check.py")
vc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vc)

statuses = []


def report(status, text):
    statuses.append(status)
    print(f"{status:8s} {text}")


def result(ok, text):
    report("PASS" if ok else "FAIL", text)


def copy_cores(tmp):
    """A throwaway copy of py/cores/ as a package folder: <tmp>/pkg/cores."""
    dest = Path(tmp) / "pkg" / "cores"
    shutil.copytree(CORES, dest, ignore=shutil.ignore_patterns("__pycache__"))
    return dest


def import_cores(pkg_dir):
    """Import the cores package from pkg_dir in a fresh interpreter; (exit code, stderr)."""
    code = f"import sys; sys.path.insert(0, {str(pkg_dir)!r}); import cores"
    proc = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True)
    return proc.returncode, proc.stderr


def source_bytes(entry):
    """The source file's bytes at the recorded commit, or None if its repo is not here."""
    folder = MAD / entry["source_folder"]
    if not (folder / ".git").exists():
        return None
    proc = subprocess.run(["git", "-C", str(folder), "--no-optional-locks", "cat-file", "blob",
                           f"{entry['commit']}:{entry['source_path']}"], capture_output=True)
    return proc.stdout if proc.returncode == 0 else b""


def expected_bytes(entry, source, symbols=None):
    """What the vendored file must hold: the source itself, or the lift rendered from it
    (with `symbols` in place of the entry's own, for a near miss)."""
    if "symbols" not in entry:
        return source
    text = vc.render_lift(Path(entry["path"]).name, entry["source_folder"], entry["source_path"],
                          source.decode("utf-8"), symbols or entry["symbols"],
                          entry.get("imports", ()))
    return text.encode("utf-8")


def one_byte_changed(source, declaration):
    """The source with one space added right after `declaration`, a regex for the first
    lifted symbol's declaration, so a lift rendered from it must differ from the file."""
    m = re.search(declaration.encode("utf-8"), source, re.MULTILINE)
    return source[:m.end()] + b" " + source[m.end():]


def check_js_lifts():
    """V4: every lifted JavaScript file in app/VENDORED.json."""
    entries = vc.load_manifest(APP)["files"]
    node = shutil.which("node")
    for entry in entries:
        name = entry["path"]
        path = APP / name
        found = vc.sha256_file(path) if path.is_file() else "missing"
        result(found == entry["sha256"], f"V4 app/{name} matches app/VENDORED.json")
        source = source_bytes(entry)
        if source is None:
            report("NOT RUN", f"V4 app/{name}: MAD/{entry['source_folder']} is not here")
            continue
        if source == b"":
            result(False, f"V4 app/{name}: {entry['source_path']} not found at {entry['commit'][:7]}")
            continue

        def render(src):
            return vc.render_js_lift(name, entry["source_folder"], entry["source_path"],
                                     src.decode("utf-8"), entry["symbols"],
                                     entry["shared"]).encode("utf-8")

        lifted = path.read_bytes()
        same = lifted == render(source)
        result(same, f"V4 app/{name} is {entry['source_folder']}/{entry['source_path']} "
               f"@ {entry['commit'][:7]} (lifted: {', '.join(entry['symbols'])})")
        if same:
            declaration = "^" + vc.JS_DECLARATION.format(name=re.escape(entry["symbols"][0]))
            result(render(one_byte_changed(source, declaration)) != lifted,
                   f"V4 near miss: app/{name} rendered from a source one byte longer does not match")
        if node is None:
            report("NOT RUN", f"V4 app/{name} parses: node not found")
            continue
        parses = subprocess.run([node, "--check", str(path)], capture_output=True).returncode == 0
        result(parses, f"V4 app/{name} parses as JavaScript (node --check)")
        if parses:
            # Near miss: the same file with its last line cut off must fail to parse.
            with tempfile.TemporaryDirectory() as tmp:
                cut = Path(tmp) / name
                cut.write_bytes(b"\n".join(lifted.rstrip(b"\n").split(b"\n")[:-2]) + b"\n")
                broken = subprocess.run([node, "--check", str(cut)], capture_output=True).returncode != 0
            result(broken, f"V4 near miss: app/{name} with its last two lines cut does not parse")


def main():
    entries = vc.load_manifest(CORES)["files"]
    victim = entries[0]["path"]   # the file the near misses tamper with

    with tempfile.TemporaryDirectory() as tmp:
        # V1: hashes, and a one-byte change is caught.
        problems = vc.check_vendored(CORES)
        result(not problems, f"V1 {len(entries)} vendored files match VENDORED.json"
               + (f": {problems}" if problems else ""))
        if not problems:
            copy = copy_cores(tmp)
            with open(copy / victim, "ab") as f:
                f.write(b"\n")
            caught = vc.check_vendored(copy)
            result(any(victim in p for p in caught), f"V1 near miss: one byte added to {victim} is caught")

        # V2: the package refuses to import a tampered copy, naming the file.
        with tempfile.TemporaryDirectory() as tmp2:
            copy = copy_cores(tmp2)
            code, err = import_cores(copy.parent)
            result(code == 0, "V2 an untouched copy of the cores package imports")
            if code == 0:
                with open(copy / victim, "ab") as f:
                    f.write(b"\n")
                code, err = import_cores(copy.parent)
                result(code != 0 and "VendoredCoreError" in err and victim in err,
                       f"V2 near miss: importing with {victim} changed refuses, naming it")

    # V3: each file against its source repo at the recorded commit.
    for i, entry in enumerate(entries):
        source = source_bytes(entry)
        name = entry["path"]
        if source is None:
            report("NOT RUN", f"V3 {name}: MAD/{entry['source_folder']} is not here")
            continue
        if source == b"":
            result(False, f"V3 {name}: {entry['source_path']} not found at {entry['commit'][:7]}")
            continue
        vendored = (CORES / name).read_bytes()
        expected = expected_bytes(entry, source)
        result(vendored == expected, f"V3 {name} is {entry['source_folder']}/{entry['source_path']} "
               f"@ {entry['commit'][:7]}" + (" (lifted: " + ", ".join(entry["symbols"]) + ")"
                                             if "symbols" in entry else ""))
        if vendored != expected:
            continue   # a near miss would prove nothing next to a failing case
        if "symbols" in entry and len(entry["symbols"]) > 1:
            # Near miss: the lift rendered with its functions in the wrong order must not match.
            wrong = expected_bytes(entry, source, entry["symbols"][::-1])
            result(wrong != vendored,
                   f"V3 near miss: {name} with its functions reordered does not match")
        elif "symbols" in entry:
            # One function cannot be reordered: render it from a source one byte longer instead.
            declaration = rf"^(?:async\s+)?def\s+{re.escape(entry['symbols'][0])}\b"
            wrong = expected_bytes(entry, one_byte_changed(source, declaration))
            result(wrong != vendored,
                   f"V3 near miss: {name} rendered from a source one byte longer does not match")
        else:
            # Near miss: the same lookup and comparison, pointed at another entry's source.
            other = next(e for e in entries[i + 1:] + entries[:i] if "symbols" not in e)
            other_source = source_bytes(other)
            if other_source is None:
                report("NOT RUN", f"V3 near miss for {name}: MAD/{other['source_folder']} is not here")
            else:
                result(other_source != vendored,
                       f"V3 near miss: {name} does not match {other['source_folder']}/{other['source_path']}")

    check_js_lifts()

    print()
    if "FAIL" in statuses:
        print("RESULT: FAIL")
        return 1
    if "NOT RUN" in statuses:
        print("RESULT: NOT RUN (some checks could not run; this is not a pass)")
        return 2
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
