#!/usr/bin/env python3
# test_offline.py - the offline copy's file list and the install manifest (M5), without
# a browser.
#
#   O1  sw.js's precache list is current: every listed file's sha256 matches the file on
#       disk and VERSION is the list's hash (py/build_precache.py --check)
#   O2  the list holds every file the page loads, found from the page's own references,
#       not from the list's rule: index.html and its manifest, the manifest's icons, the
#       fonts in its @font-face rules, the module graph from index.html (app/ and
#       pyodide/pyodide.mjs), the py/ files recqc_boot.mjs copies into Pyodide (named
#       there and in py/cores/VENDORED.json), and every file under pyodide/
#   O3  the list holds nothing the page does not load: no tests, no build scripts
#   O4  the manifest installs from any folder (start_url and scope "./", standalone) and
#       each icon is a PNG of its stated size, one of them maskable
#
# Each check sits next to its near miss (builds:grader-negative-control). What it cannot
# show: that the service worker installs, serves and survives the network going away;
# tests/headless_capture.py --offline checks that in Chrome.
#
# Run:   python3 tests/test_offline.py
# Exit:  0 pass, 1 fail
import sys
sys.dont_write_bytecode = True   # leave no __pycache__ in py/

import json
import re
import struct
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "py"))
import build_precache  # noqa: E402

statuses = []


def result(ok, text):
    statuses.append("PASS" if ok else "FAIL")
    print(f"{'PASS' if ok else 'FAIL':8s} {text}")


def png_size(path):
    head = path.read_bytes()[:24]
    return struct.unpack(">II", head[16:24]) if head[:8] == b"\x89PNG\r\n\x1a\n" else None


def module_graph(start):
    """Every module reachable from index.html's imports, as repo-relative paths: static
    imports and new URL(..., import.meta.url) workers, followed file by file."""
    refs = re.compile(r'''(?:from\s+|new URL\(\s*)"(\.{1,2}/[^"]+)"''')
    found, todo = set(), [start]
    while todo:
        page = todo.pop()
        for rel in refs.findall((REPO / page).read_text(encoding="utf-8")):
            path = (REPO / page).parent.joinpath(rel).resolve().relative_to(REPO).as_posix()
            if path not in found:
                found.add(path)
                todo.append(path)
    return found


def page_loads():
    """{file: why} for every file the page loads, from the page's own references."""
    html = (REPO / "index.html").read_text(encoding="utf-8")
    needed = {"index.html": "the page"}
    manifest = re.search(r'<link rel="manifest" href="([^"]+)"', html)
    if manifest:
        needed[manifest.group(1)] = "index.html's manifest link"
        for icon in json.loads((REPO / manifest.group(1)).read_text(encoding="utf-8"))["icons"]:
            needed[icon["src"]] = "an icon in the manifest"
    for font in re.findall(r'url\("(fonts/[^"]+)"\)', html):
        needed[font] = "an @font-face in index.html"
    for module in module_graph("index.html"):
        needed[module] = "the module graph from index.html"
    boot = (REPO / "app/recqc_boot.mjs").read_text(encoding="utf-8")
    py_files = re.search(r"const pyFiles = \[(.*?)\]", boot, re.S).group(1)
    for rel in re.findall(r'"([^"]+\.(?:py|json))"', py_files):
        needed[f"py/{rel}"] = "named in recqc_boot.mjs"
    for f in json.loads((REPO / "py/cores/VENDORED.json").read_text(encoding="utf-8"))["files"]:
        needed[f"py/cores/{f['path']}"] = "a vendored core recqc_boot.mjs copies"
    for f in sorted((REPO / "pyodide").iterdir()):
        if f.is_file() and f.name != ".DS_Store":
            needed[f"pyodide/{f.name}"] = "the vendored runtime"
    return needed


def main():
    sw = (REPO / "sw.js").read_text(encoding="utf-8")
    entries = build_precache.precache()
    current = build_precache.with_block(sw, build_precache.generated_block(entries)) == sw
    result(current, f"O1 sw.js's list is current: {len(entries)} files, VERSION {build_precache.version(entries)}")
    nudged = [(p, ("0" if s[0] != "0" else "1") + s[1:]) if p == "index.html" else (p, s) for p, s in entries]
    stale = build_precache.with_block(sw, build_precache.generated_block(nudged)) != sw
    result(stale, "O1 near miss: a list holding index.html's old hash is caught as stale")
    result(build_precache.version(entries) != build_precache.version(entries[1:]),
           "O1 near miss: a list missing a file names another cache")

    listed = {p for p, _ in entries}
    needed = page_loads()

    def missing_from(files):
        return sorted(p for p in needed if p not in files)

    missing = missing_from(listed)
    result(not missing, f"O2 the list holds all {len(needed)} files the page loads"
           + (": missing " + ", ".join(f"{p} ({needed[p]})" for p in missing) if missing else ""))
    for drop in ("app/platform_capture.mjs", "pyodide/pyodide.asm.wasm", "py/cores/_vendor_check.py",
                 "fonts/ibm-plex-mono-latin-500-normal.woff2", "icons/icon-maskable-512.png"):
        result(drop in missing_from(listed - {drop}),
               f"O2 near miss: a list without {drop} is caught ({needed.get(drop, 'NOT FOUND as a load')})")

    extra = sorted(p for p in listed if p not in needed)
    result(not extra, "O3 the list holds only what the page loads"
           + (": also " + ", ".join(extra) if extra else ""))
    strays = ["tests/test_offline.py", "py/build_precache.py", "py/vendor_cores.sh", "sw.js", "README.md",
              "app/VENDORED.json"]
    result(not any(build_precache.listed(p) for p in strays) and build_precache.listed("py/cores/tiers.py"),
           "O3 near miss: the list's rule refuses tests, build scripts, sw.js, the README and app/VENDORED.json, "
           "and takes a core")

    manifest = json.loads((REPO / "manifest.webmanifest").read_text(encoding="utf-8"))
    icons_ok = []
    for icon in manifest["icons"]:
        w, h = (int(x) for x in icon["sizes"].split("x"))
        icons_ok.append(png_size(REPO / icon["src"]) == (w, h) and icon["type"] == "image/png")
    result(manifest["start_url"] == "./" and manifest["scope"] == "./" and manifest["display"] == "standalone"
           and all(icons_ok) and any(i.get("purpose") == "maskable" for i in manifest["icons"])
           and any(i["sizes"] == "512x512" and i.get("purpose") == "any" for i in manifest["icons"]),
           f"O4 the manifest: start_url and scope ./, standalone, {len(icons_ok)} PNG icons of their stated sizes, one maskable")
    first = manifest["icons"][0]
    w = int(first["sizes"].split("x")[0])
    result(png_size(REPO / first["src"]) != (2 * w, 2 * w),
           f"O4 near miss: {first['src']} stated at twice its size is caught")

    print()
    print("RESULT: FAIL" if "FAIL" in statuses else "RESULT: PASS")
    return 1 if "FAIL" in statuses else 0


if __name__ == "__main__":
    sys.exit(main())
