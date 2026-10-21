#!/bin/bash
# vendor_pyodide.sh - refresh the vendored Pyodide runtime and wheels under pyodide/.
#
# The ONLY way the files under pyodide/ change. It downloads one pinned Pyodide
# release and the wheels RecQC loads, checks every sha256, and only then replaces
# pyodide/ and writes pyodide/VENDORED.json (source URL, size, sha256 per file).
#
#   runtime     pyodide.mjs, pyodide.asm.mjs, pyodide.asm.wasm, python_stdlib.zip,
#               pyodide-lock.json   <- the release's core archive, pinned by sha256
#   wheels      numpy, scipy, pyyaml, soundfile, soxr, future and whatever the lock
#               says they depend on <- the release CDN, each checked against the lock
#   pyloudnorm  0.1.1, pure Python and not part of Pyodide <- PyPI, pinned by sha256
#               (the version the live platform runs; it needs `future`)
#
# It REFUSES, and changes nothing, if a download fails or any sha256 differs.
# Downloads are only read as data: the archive is checked before it is unpacked,
# only the named files come out of it, and the lock is parsed with python3 -I.
#
# Usage:  py/vendor_pyodide.sh
# Exit:   0 vendored, 1 refused or failed

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd -P)"   # .../RecQC standalone/py
REPO="$(dirname "$HERE")"                   # .../RecQC standalone
DEST="$REPO/pyodide"
PYTHON_BIN="${PYTHON_BIN:-python3}"

PYODIDE_VERSION="314.0.7"
CORE_URL="https://github.com/pyodide/pyodide/releases/download/$PYODIDE_VERSION/pyodide-core-$PYODIDE_VERSION.tar.bz2"
CORE_SHA256="2abdcc2e35208af406e07724cffa85bc582ced97e9028383ecf5462541393f95"
CDN="https://cdn.jsdelivr.net/pyodide/v$PYODIDE_VERSION/full"
RUNTIME_FILES="pyodide.mjs pyodide.asm.mjs pyodide.asm.wasm python_stdlib.zip pyodide-lock.json"
PACKAGES="numpy scipy pyyaml soundfile soxr future"   # lock names; their dependencies are added
PYLOUDNORM_URL="https://files.pythonhosted.org/packages/58/f5/6724805521ab4e723a12182f92374031032aff28a8a89dc8505c52b79032/pyloudnorm-0.1.1-py3-none-any.whl"
PYLOUDNORM_SHA256="d7f12ebdd097a464d87ce2878fc4d942f15f8233e26cc03f33fefa226f869a14"

refuse() {
  echo "REFUSED: $1" >&2
  echo "Nothing under pyodide/ was changed." >&2
  exit 1
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir "$WORK/archive" "$WORK/new"
: > "$WORK/rows.txt"   # one 'file|source url' line per vendored file

fetch() {   # fetch <url> <out file> <expected sha256>
  curl -fsSL --retry 2 -o "$2" "$1" || refuse "download failed: $1"
  local got
  got="$(shasum -a 256 "$2" | cut -d' ' -f1)"
  [ "$got" = "$3" ] || refuse "sha256 mismatch for $(basename "$2"): expected ${3:0:12}, got ${got:0:12}"
  echo "$(basename "$2")|$1" >> "$WORK/rows.txt"
}

# --- 1. The runtime, from the release's core archive (checked before it is unpacked) ---
echo "Pyodide $PYODIDE_VERSION runtime ..."
curl -fsSL --retry 2 -o "$WORK/core.tar.bz2" "$CORE_URL" || refuse "download failed: $CORE_URL"
got="$(shasum -a 256 "$WORK/core.tar.bz2" | cut -d' ' -f1)"
[ "$got" = "$CORE_SHA256" ] || refuse "sha256 mismatch for the core archive: got ${got:0:12}"
members=""
for f in $RUNTIME_FILES; do members="$members pyodide/$f"; done
# shellcheck disable=SC2086  # the member names are deliberately split into separate words
tar -xjf "$WORK/core.tar.bz2" -C "$WORK/archive" $members
for f in $RUNTIME_FILES; do
  mv "$WORK/archive/pyodide/$f" "$WORK/new/$f"
  echo "$f|$CORE_URL" >> "$WORK/rows.txt"
done

# --- 2. The wheels the lock names for PACKAGES, plus their dependencies ---
# shellcheck disable=SC2086
wheels="$("$PYTHON_BIN" -I - "$WORK/new/pyodide-lock.json" $PACKAGES <<'PY'
import json, sys
lock = json.load(open(sys.argv[1], encoding="utf-8"))["packages"]
todo, seen = list(sys.argv[2:]), []
while todo:                      # follow "depends" so every needed wheel is listed once
    name = todo.pop(0)
    if name in seen:
        continue
    if name not in lock:
        sys.exit(f"package not in the lock: {name}")
    seen.append(name)
    todo.extend(lock[name].get("depends", []))
for name in seen:
    print(lock[name]["file_name"], lock[name]["sha256"])
PY
)" || refuse "could not read the package list from pyodide-lock.json"
while read -r file sha; do
  echo "  $file"
  fetch "$CDN/$file" "$WORK/new/$file" "$sha"
done <<< "$wheels"

# --- 3. pyloudnorm, from PyPI ---
echo "  $(basename "$PYLOUDNORM_URL") (PyPI)"
fetch "$PYLOUDNORM_URL" "$WORK/new/$(basename "$PYLOUDNORM_URL")" "$PYLOUDNORM_SHA256"

# --- 4. The manifest, then swap the new folder in ---
"$PYTHON_BIN" -I - "$WORK/new" "$PYODIDE_VERSION" "$WORK/rows.txt" <<'PY'
import hashlib, json, sys
from pathlib import Path
new, version, rows = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
files = []
for line in rows.read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    path, source = line.split("|", 1)
    data = (new / path).read_bytes()
    files.append({"path": path, "source": source, "bytes": len(data),
                  "sha256": hashlib.sha256(data).hexdigest()})
doc = {
    "written_by": "py/vendor_pyodide.sh",
    "pyodide_version": version,
    "note": ("Written by py/vendor_pyodide.sh. Do not edit by hand. The browser checks the "
             "lock's wheels against pyodide-lock.json itself; Node does not, so the parity "
             "test checks every file below before it starts Pyodide. RecQC checks the "
             "pyloudnorm wheel against this file before installing it."),
    "files": files,
}
(new / "VENDORED.json").write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
print(f"{len(files)} files, {sum(f['bytes'] for f in files):,} bytes")
PY

chmod 644 "$WORK/new"/*   # the archive marks some files executable; none of them is

if [ -d "$DEST" ]; then
  rm -rf "$DEST.old"
  mv "$DEST" "$DEST.old"
fi
mv "$WORK/new" "$DEST"
rm -rf "$DEST.old"

echo "Wrote pyodide/ and pyodide/VENDORED.json."
echo "Review the change with:  git status -- pyodide/"
