#!/bin/bash
# vendor_cores.sh - refresh RecQC's vendored platform files under py/cores/ and app/.
#
# The ONLY way the vendored files under py/cores/, and the lifted app/platform_capture.mjs,
# change. It copies them from the sibling MAD source repos and rewrites py/cores/VENDORED.json
# and app/VENDORED.json (source folder, repo, commit, sha256 per file). The pattern is
# MAD/Intake Scanner/vendor_cores.sh.
#
#   level_check_phrase_core.py        <- MAD/Level Check Phrase Test
#   noise_profile_scanner_core.py     <- MAD/Noise Profile Scanner
#   configs/noise/platform_band.yaml  <- MAD/Noise Profile Scanner/configs
#   rt60_analyzer_core_v5.py          <- MAD/RT60 Analyzer
#   configs/rt60/<profile>.yaml       <- MAD/RT60 Analyzer/configs (platform_band, strict, forgiving)
#   screening_pipeline_core.py        <- MAD/Screening Pipeline (the platform's stage gates)
#   provenance.py, tiers.py           <- MAD/Talent Platform/app
#   platform_ceiling.py               <- MAD/Talent Platform/app/audition_reviews.py, only
#                                        _env_ceiling and _device_ceiling, lifted verbatim
#   platform_ingest.py                <- MAD/Talent Platform/app/cores.py, only
#                                        _derive_working and the four imports it uses
#
# and, for the page, into app/ with its own manifest app/VENDORED.json:
#
#   platform_capture.mjs              <- MAD/Talent Platform/app/static/index.html, only
#                                        the six capture declarations named in JS_LIFTS
#
# It REFUSES, and changes nothing, when a source file is not committed at its repo's
# HEAD or has uncommitted changes of its own. What it copies is the committed file at
# HEAD, so the commit recorded in the manifest is exactly the code that was copied.
# Uncommitted work elsewhere in a source repo does not block it (per-file rule).
#
# The source repos are only read. Every git call there passes --no-optional-locks.
# It runs only where the MAD source repos sit next to this repo; a fork keeps the
# vendored copies as they are.
#
# Usage:  py/vendor_cores.sh
# Exit:   0 vendored, 1 refused or failed

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd -P)"   # .../RecQC standalone/py
REPO="$(dirname "$HERE")"                   # .../RecQC standalone
MAD="$(dirname "$REPO")"                    # .../MAD, where the source repos sit
DEST="$HERE/cores"
APP="$REPO/app"
PYTHON_BIN="${PYTHON_BIN:-python3}"         # _vendor_check.py is stdlib only

# One line per vendored file:  path under cores/ | source folder under MAD | path in that repo
FILES=(
  "level_check_phrase_core.py|Level Check Phrase Test|level_check_phrase_core.py"
  "noise_profile_scanner_core.py|Noise Profile Scanner|noise_profile_scanner_core.py"
  "configs/noise/platform_band.yaml|Noise Profile Scanner|configs/platform_band.yaml"
  "rt60_analyzer_core_v5.py|RT60 Analyzer|rt60_analyzer_core_v5.py"
  "configs/rt60/platform_band.yaml|RT60 Analyzer|configs/platform_band.yaml"
  "configs/rt60/strict.yaml|RT60 Analyzer|configs/strict.yaml"
  "configs/rt60/forgiving.yaml|RT60 Analyzer|configs/forgiving.yaml"
  "screening_pipeline_core.py|Screening Pipeline|screening_pipeline_core.py"
  "provenance.py|Talent Platform|app/provenance.py"
  "tiers.py|Talent Platform|app/tiers.py"
)

# One line per lifted file:  path under cores/ | source folder | path in that repo | function names
#                            [| import lines from that file the functions use, ';'-separated]
LIFTS=(
  "platform_ceiling.py|Talent Platform|app/audition_reviews.py|_env_ceiling,_device_ceiling"
  "platform_ingest.py|Talent Platform|app/cores.py|_derive_working|from pathlib import Path;import numpy as np;import soundfile as sf;import soxr"
)

# One line per lifted JavaScript file:  path under app/ | source folder | path in that repo |
#                                       declaration names | page variables they share
JS_LIFTS=(
  "platform_capture.mjs|Talent Platform|app/static/index.html|WORKLET_CODE,encodeWav24,getMicConstraints,checkMicProcessing,browserFingerprint,IS_MOBILE|procStatus,lastBrowserFingerprint,audioCtx"
)

refuse() {
  echo "REFUSED: $1" >&2
  echo "Nothing under py/cores/ was changed." >&2
  exit 1
}

src_git() {   # read-only git inside one source folder:  src_git <folder> <git args...>
  local folder="$1"
  shift
  git -C "$MAD/$folder" --no-optional-locks "$@"
}

"$PYTHON_BIN" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2> /dev/null \
  || refuse "$PYTHON_BIN is missing or older than Python 3.10. It writes the manifest."
[ -f "$DEST/_vendor_check.py" ] || refuse "py/cores/_vendor_check.py not found."
command -v node > /dev/null || refuse "node not found. It checks that the lifted JavaScript parses."

check_source() {   # check_source <folder> <path>: refuse unless committed at HEAD and clean
  local folder="$1" path="$2" dirty
  [ -d "$MAD/$folder" ] || refuse "source folder not found: MAD/$folder"
  src_git "$folder" rev-parse --verify --quiet HEAD > /dev/null \
    || refuse "MAD/$folder is not a git repo with a commit, so there is no commit to record."
  src_git "$folder" cat-file -e "HEAD:$path" 2> /dev/null \
    || refuse "$path is not committed in MAD/$folder."
  # Only this file's own status counts; other work in the repo does not block.
  dirty="$(src_git "$folder" status --porcelain -- "$path")"
  [ -z "$dirty" ] || refuse "MAD/$folder/$path has uncommitted changes:
$dirty
Commit or stash them there first, so the recorded commit is the code that gets copied."
}

repo_name() {   # the source repo's name, from its origin remote (empty if none)
  local url
  url="$(src_git "$1" remote get-url origin 2> /dev/null || true)"
  [ -n "$url" ] && basename "${url%.git}" || true
}

# --- 1. Check every source file before copying anything, so a refusal changes nothing ---
for line in "${FILES[@]}"; do
  IFS='|' read -r dest folder path <<< "$line"
  check_source "$folder" "$path"
done
for line in "${LIFTS[@]}" "${JS_LIFTS[@]}"; do
  IFS='|' read -r dest folder path names extra <<< "$line"
  check_source "$folder" "$path"
done

# --- 2. Copy the committed bytes (or lift the named functions), then write the manifests ---
rows=""
summary=""
for line in "${FILES[@]}"; do
  IFS='|' read -r dest folder path <<< "$line"
  mkdir -p "$(dirname "$DEST/$dest")"
  src_git "$folder" cat-file blob "HEAD:$path" > "$DEST/$dest"
  commit="$(src_git "$folder" rev-parse HEAD)"
  rows="$rows$dest|$folder|$path|$(repo_name "$folder")|$commit"$'\n'
  summary="$summary$(printf '  %-34s <- %s @ %s' "$dest" "$folder" "${commit:0:7}")"$'\n'
done
for line in "${LIFTS[@]}"; do
  IFS='|' read -r dest folder path names imports <<< "$line"
  # The import lines are passed only when the lift has some (an empty argument is not "none").
  src_git "$folder" cat-file blob "HEAD:$path" \
    | "$PYTHON_BIN" -B "$DEST/_vendor_check.py" lift "$DEST/$dest" "$folder" "$path" "$names" \
        ${imports:+"$imports"}
  commit="$(src_git "$folder" rev-parse HEAD)"
  rows="$rows$dest|$folder|$path|$(repo_name "$folder")|$commit|$names${imports:+|$imports}"$'\n'
  summary="$summary$(printf '  %-34s <- %s @ %s (%s)' "$dest" "$folder" "${commit:0:7}" "$names")"$'\n'
done

printf '%s' "$rows" | "$PYTHON_BIN" -B "$DEST/_vendor_check.py" write "$DEST"

js_rows=""
js_summary=""
for line in "${JS_LIFTS[@]}"; do
  IFS='|' read -r dest folder path names shared <<< "$line"
  src_git "$folder" cat-file blob "HEAD:$path" \
    | "$PYTHON_BIN" -B "$DEST/_vendor_check.py" lift-js "$APP/$dest" "$folder" "$path" "$names" "$shared"
  commit="$(src_git "$folder" rev-parse HEAD)"
  js_rows="$js_rows$dest|$folder|$path|$(repo_name "$folder")|$commit|$names|$shared"$'\n'
  js_summary="$js_summary$(printf '  %-34s <- %s @ %s (%s)' "$dest" "$folder" "${commit:0:7}" "$names")"$'\n'
done

printf '%s' "$js_rows" | "$PYTHON_BIN" -B "$DEST/_vendor_check.py" write-js "$APP"

# --- 3. Run the start check once, on what was just written, and parse the JavaScript ---
"$PYTHON_BIN" -B "$DEST/_vendor_check.py" check "$DEST" \
  || { echo "FAILED: the start check does not pass on the new copies." >&2; exit 1; }
for line in "${JS_LIFTS[@]}"; do
  IFS='|' read -r dest folder path names shared <<< "$line"
  node --check "$APP/$dest" \
    || { echo "FAILED: app/$dest does not parse as JavaScript." >&2; exit 1; }
done

echo "Vendored into py/cores/:"
printf '%s' "$summary"
echo "Vendored into app/:"
printf '%s' "$js_summary"
echo "Wrote py/cores/VENDORED.json and app/VENDORED.json. Start check: OK. JavaScript parses."
echo "Review the change with:  git diff --stat -- py/cores/ app/"
