#!/usr/bin/env python3
# headless_capture.py - the whole page path in headless Chrome, with a fake mic.
#
# Serves this repo on a local port and opens index.html?selftest in headless Chrome
# with Chrome's fake media device (the mic permission is granted without a prompt). The
# page's selftest turns the mic on, records takes through the platform's meter worklet,
# encodes them with the platform's encoder, scores them in the Pyodide module worker, and
# writes SELFTEST PASS or SELFTEST FAIL into the page title. This script reads the title
# from Chrome's DevTools HTTP endpoint (/json/list), so it needs nothing beyond the
# standard library.
#
# Two fake mics:
#   corpus (the default, M3; claps M4)  a file built here from one Testing corpus folder:
#       the first 10 s of its phrase take, its room tone to 20 s, its clap take, then room
#       tone to 60 s; the room tone is played forward and backward in turn so no splice
#       clicks inside it. The page runs ?selftest=full: a level take inside the speech, a
#       room-tone take from 12 s, the save payload, a clap take from 19.5 s that must stop
#       itself, the ceiling card, and re-scores under other profiles. The quiet room and
#       room reflection must both run.
#   --tone (M2)  Chrome's own synthetic tone. The level take is scored; a tone can fairly
#       fail the level gate, and then the quiet room does not run (the platform's order).
#
# --offline (M5) checks the offline copy, on the tone. The page opens with ?sw, so it
# registers sw.js on this local server, and its pass waits for the copy: the cache must
# be named for sw.js's VERSION and hold every file on its list. Then the server is shut
# and the page opened again in a new tab: it must load, boot the engine, score a take and
# pass with the network gone, and say it was served from the offline copy. The server
# sends Cache-Control: no-store, so Chrome's HTTP cache cannot stand in for the copy.
#
# What a pass shows: the module worker boots, the lifted capture code runs in a real
# browser, and captured takes come back scored by the cores. What it cannot show: a real
# mic, a real room, the folder picker, or how Chrome treats your interface; those are
# checked by hand.
#
# Run:   python3 tests/headless_capture.py [--tone | --offline] [--corpus <dir>] [--chrome <path>] [--timeout 180]
# Exit:  0 pass, 1 fail, 2 not run (Chrome or the corpus not found)
import argparse
import functools
import html
import http.server
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "py"))
import build_precache  # noqa: E402

CHROME ="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CORPUS = REPO.parent / "Screening Pipeline" / "Testing corpus"
# The page's ?selftest=full timing (index.html, SELFTEST_TIMES): speech must cover the
# level take, room tone the room-tone take, and the claps start after it; room tone
# after the claps covers the clap take's 30 s limit, with margin for when the fake mic
# starts.
SPEECH_SECONDS, CLAPS_AT_SECONDS, TOTAL_SECONDS = 10, 20, 60


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class NoStoreHandler(QuietHandler):
    """--offline: nothing goes into Chrome's HTTP cache, so only the offline copy is left."""
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def selftest_target(devtools_port, target_id=None):
    """The selftest page's DevTools target once its title holds a result (that tab only,
    if target_id is given), else None."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{devtools_port}/json/list", timeout=2) as r:
            targets = json.load(r)
    except (OSError, ValueError):
        return None   # Chrome is still starting
    for t in targets:
        if t.get("type") == "page" and t.get("title", "").startswith("SELFTEST") \
                and target_id in (None, t.get("id")):
            return t
    return None


def wait_title(devtools_port, timeout, target_id=None):
    """(title, target id), or ("", None) if no result came within timeout seconds."""
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        time.sleep(0.5)
        t = selftest_target(devtools_port, target_id)
        if t:
            return html.unescape(t["title"]), t["id"]   # the endpoint escapes the title as HTML
    return "", None


def reopen(devtools_port, url, old_id):
    """Open url in a new tab, then close the old one. Returns the new tab's target id."""
    # Quoted, or the endpoint would take the page's own & as its own query's.
    new = urllib.request.Request(f"http://127.0.0.1:{devtools_port}/json/new?{urllib.parse.quote(url, safe='')}",
                                 method="PUT")
    with urllib.request.urlopen(new, timeout=5) as r:
        new_id = json.load(r)["id"]
    urllib.request.urlopen(f"http://127.0.0.1:{devtools_port}/json/close/{old_id}", timeout=5).close()
    return new_id


def read_frames(path):
    """(params, frames) of a PCM WAV."""
    with wave.open(str(path)) as w:
        return w.getparams(), w.readframes(w.getnframes())


def pick_folder(corpus):
    """The corpus folder for the fake mic: of those whose stored records pass the level
    check, the quiet room and room reflection (the June records, scored under the cores'
    defaults), the one with the most claps detected, then the most SNR. The claps let the
    clap take stop on its count; the SNR headroom keeps the room out of Platform Band's
    lo-fi band, where room reflection would not run."""
    ranked = []
    for folder in sorted(p for p in corpus.iterdir() if p.is_dir()):
        result = folder / "screening_result.json"
        if not result.is_file():
            continue
        record = json.loads(result.read_text(encoding="utf-8"))
        verdicts = {s["stage"]: s["verdict"] for s in record["stage_results"]}
        if all(verdicts.get(s) == "PASS" for s in ("level_check", "noise_profile", "rt60")):
            claps = record["stages"]["rt60"]["summary"]["n_claps_total"]
            ranked.append((-claps, -record["stages"]["noise_profile"]["snr_db"], folder.name, folder))
    if not ranked:
        raise FileNotFoundError(f"no folder in {corpus} passes the level check, the quiet room and room reflection")
    return min(ranked)[-1]


def build_fake_audio(corpus, dest):
    """Write the corpus fake-mic file to dest. Returns the corpus folder it came from
    (pick_folder)."""
    folder = pick_folder(corpus)
    params, phrase = read_frames(next(folder.glob("*_phrase.wav")))
    takes = [phrase]
    for role in ("noise", "clap"):
        role_params, frames = read_frames(next(folder.glob(f"*_{role}.wav")))
        if (role_params.nchannels, role_params.sampwidth, role_params.framerate) != \
                (params.nchannels, params.sampwidth, params.framerate) or params.nchannels != 1:
            raise ValueError(f"{folder.name}: the phrase and {role} takes are not the same mono format")
        takes.append(frames)
    if params.sampwidth == 3:
        # Chrome's fake device rejects 24-bit PCM ("WAV data could be read but is not
        # valid"), so keep each sample's two high bytes: 16-bit, with truncation noise
        # near -100 dBFS, below the corpus room tones.
        takes = [b"".join(x[i + 1:i + 3] for i in range(0, len(x), 3)) for x in takes]
        params = params._replace(sampwidth=2)
    phrase, noise, claps = takes
    width = params.sampwidth   # bytes per frame (mono)
    speech = phrase[:SPEECH_SECONDS * params.framerate * width]
    # Room tone forward, then backward: each turn starts on the sample the last one ended
    # on, so the loop has no step a click detector could catch.
    backward = b"".join(noise[i:i + width] for i in range(len(noise) - width, -1, -width))

    def room_tone(seconds):
        tone, turn = bytearray(), 0
        while len(tone) < seconds * params.framerate * width:
            tone += noise if turn % 2 == 0 else backward
            turn += 1
        return bytes(tone[:int(seconds * params.framerate) * width])

    before = room_tone(CLAPS_AT_SECONDS - SPEECH_SECONDS)
    after = room_tone(TOTAL_SECONDS - CLAPS_AT_SECONDS - len(claps) / (params.framerate * width))
    with wave.open(str(dest), "wb") as w:
        w.setparams(params)
        w.writeframes(speech + before + claps + after)
    return folder


def main():
    ap = argparse.ArgumentParser(description="The page path in headless Chrome with a fake mic")
    ap.add_argument("--tone", action="store_true", help="use Chrome's own tone (the M2 path)")
    ap.add_argument("--offline", action="store_true",
                    help="on the tone, check the offline copy: install it, shut the server, open again")
    ap.add_argument("--corpus", default=str(CORPUS))
    ap.add_argument("--chrome", default=CHROME)
    ap.add_argument("--timeout", type=float, default=180.0, help="seconds to wait for each result")
    args = ap.parse_args()
    if not Path(args.chrome).exists():
        print(f"NOT RUN  Chrome not found at {args.chrome}")
        return 2

    scratch = Path(tempfile.mkdtemp(prefix="recqc-chrome-"))
    flags, url_query = [], "selftest"
    if args.offline:
        entries = build_precache.precache()
        copy = f"offline copy recqc-{build_precache.version(entries)} ({len(entries)} files)"
        url_query = "selftest&sw"
        print(f"Fake mic: Chrome's tone; expecting the {copy}")
    elif not args.tone:
        if not Path(args.corpus).is_dir():
            print(f"NOT RUN  corpus not found: {args.corpus} (--tone runs the tone-only path)")
            return 2
        fake = scratch / "fake_mic.wav"
        folder = build_fake_audio(Path(args.corpus), fake)
        # Chrome's sandbox stops the fake device reading the file: it plays silence and
        # logs "Failed to read <file> as input to the fake device. Try disabling the
        # sandbox with --no-sandbox." So this mode runs unsandboxed, on a throwaway
        # profile, opening only the page this script serves from the repo.
        flags += [f"--use-file-for-fake-audio-capture={fake}", "--no-sandbox"]
        url_query = "selftest=full"
        print(f"Fake mic: {folder.name} (first {SPEECH_SECONDS} s of the phrase, room tone, "
              f"its claps from {CLAPS_AT_SECONDS} s, room tone)")
    else:
        print("Fake mic: Chrome's tone")

    port, devtools = free_port(), free_port()
    handler = NoStoreHandler if args.offline else QuietHandler
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", port), functools.partial(handler, directory=str(REPO)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    serving = True
    url = f"http://127.0.0.1:{port}/index.html?{url_query}"
    chrome = subprocess.Popen(
        [args.chrome, "--headless=new", f"--remote-debugging-port={devtools}",
         f"--user-data-dir={scratch / 'profile'}", "--no-first-run", "--no-default-browser-check",
         "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", *flags,
         "--autoplay-policy=no-user-gesture-required", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    results, t0 = [], time.perf_counter()
    try:
        title, tab = wait_title(devtools, args.timeout)
        results.append(("", title))
        if args.offline and title.startswith("SELFTEST PASS") and copy in title:
            server.shutdown()
            server.server_close()
            serving = False
            title, _ = wait_title(devtools, args.timeout, reopen(devtools, url, tab))
            results.append(("offline", title))
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=10)
        except subprocess.TimeoutExpired:
            chrome.kill()
        if serving:
            server.shutdown()
        shutil.rmtree(scratch, ignore_errors=True)

    failed = False
    for which, title in results:
        label = f" ({which})" if which else ""
        if not title:
            print(f"FAIL     no result in the page title{label} within {args.timeout:.0f} s")
            failed = True
            continue
        passed = title.startswith("SELFTEST PASS")
        if args.offline and passed and copy not in title:
            passed, title = False, f"{title}  [expected the {copy}]"
        if which == "offline" and passed and "served from the offline copy" not in title:
            passed, title = False, f"{title}  [the page did not come from the offline copy]"
        print(f"{'PASS' if passed else 'FAIL':8s} {title}{label}")
        failed = failed or not passed
    print(f"({time.perf_counter() - t0:.1f} s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
