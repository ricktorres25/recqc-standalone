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
#   corpus (the default, M3)  a file built here from one Testing corpus folder: the first
#       10 s of its phrase take, then its room tone to 40 s, played forward and backward
#       in turn so no splice clicks inside it. The page runs ?selftest=full: a level take
#       inside the speech, a room-tone take from 12 s, the save payload, and a re-score of
#       that take under the Default profile. The quiet room must run.
#   --tone (M2)  Chrome's own synthetic tone. The level take is scored; a tone can fairly
#       fail the level gate, and then the quiet room does not run (the platform's order).
#
# What a pass shows: the module worker boots, the lifted capture code runs in a real
# browser, and captured takes come back scored by the cores. What it cannot show: a real
# mic, a real room, the folder picker, or how Chrome treats your interface; those are
# checked by hand.
#
# Run:   python3 tests/headless_capture.py [--tone] [--corpus <dir>] [--chrome <path>] [--timeout 180]
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
import urllib.request
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CORPUS = REPO.parent / "Screening Pipeline" / "Testing corpus"
# The page's ?selftest=full timing (index.html, SELFTEST_TIMES): speech must cover the
# level take, room tone the room-tone take, with margin for when the fake mic starts.
SPEECH_SECONDS, TOTAL_SECONDS = 10, 40


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def page_title(devtools_port):
    """The title of the selftest page, or "" while it has none yet."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{devtools_port}/json/list", timeout=2) as r:
            targets = json.load(r)
    except (OSError, ValueError):
        return ""   # Chrome is still starting
    for t in targets:
        if t.get("type") == "page" and t.get("title", "").startswith("SELFTEST"):
            return html.unescape(t["title"])   # the endpoint escapes the title as HTML
    return ""


def read_frames(path):
    """(params, frames) of a PCM WAV."""
    with wave.open(str(path)) as w:
        return w.getparams(), w.readframes(w.getnframes())


def build_fake_audio(corpus, dest):
    """Write the corpus fake-mic file to dest. Returns the corpus folder it came from: the
    first, in name order, whose stored records pass both the level check and the quiet
    room (the June records, scored under the cores' defaults)."""
    for folder in sorted(p for p in corpus.iterdir() if p.is_dir()):
        result = folder / "screening_result.json"
        if not result.is_file():
            continue
        verdicts = {s["stage"]: s["verdict"]
                    for s in json.loads(result.read_text(encoding="utf-8"))["stage_results"]}
        if verdicts.get("level_check") == "PASS" and verdicts.get("noise_profile") == "PASS":
            break
    else:
        raise FileNotFoundError(f"no folder in {corpus} passes both the level check and the quiet room")

    params, phrase = read_frames(next(folder.glob("*_phrase.wav")))
    noise_params, noise = read_frames(next(folder.glob("*_noise.wav")))
    if (noise_params.nchannels, noise_params.sampwidth, noise_params.framerate) != \
            (params.nchannels, params.sampwidth, params.framerate) or params.nchannels != 1:
        raise ValueError(f"{folder.name}: the phrase and noise takes are not the same mono format")
    if params.sampwidth == 3:
        # Chrome's fake device rejects 24-bit PCM ("WAV data could be read but is not
        # valid"), so keep each sample's two high bytes: 16-bit, with truncation noise
        # near -100 dBFS, below the corpus room tones.
        phrase, noise = (b"".join(x[i + 1:i + 3] for i in range(0, len(x), 3)) for x in (phrase, noise))
        params = params._replace(sampwidth=2)
    width = params.sampwidth   # bytes per frame (mono)
    speech = phrase[:SPEECH_SECONDS * params.framerate * width]
    # Room tone forward, then backward: each turn starts on the sample the last one ended
    # on, so the loop has no step a click detector could catch.
    backward = b"".join(noise[i:i + width] for i in range(len(noise) - width, -1, -width))
    tone, turn = bytearray(), 0
    while len(speech) + len(tone) < TOTAL_SECONDS * params.framerate * width:
        tone += noise if turn % 2 == 0 else backward
        turn += 1
    with wave.open(str(dest), "wb") as w:
        w.setparams(params)
        w.writeframes(speech + bytes(tone))
    return folder


def main():
    ap = argparse.ArgumentParser(description="The page path in headless Chrome with a fake mic")
    ap.add_argument("--tone", action="store_true", help="use Chrome's own tone (the M2 path)")
    ap.add_argument("--corpus", default=str(CORPUS))
    ap.add_argument("--chrome", default=CHROME)
    ap.add_argument("--timeout", type=float, default=180.0, help="seconds to wait for a result")
    args = ap.parse_args()
    if not Path(args.chrome).exists():
        print(f"NOT RUN  Chrome not found at {args.chrome}")
        return 2

    scratch = Path(tempfile.mkdtemp(prefix="recqc-chrome-"))
    flags, url_query = [], "selftest"
    if not args.tone:
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
        print(f"Fake mic: {folder.name} (first {SPEECH_SECONDS} s of the phrase, then room tone)")
    else:
        print("Fake mic: Chrome's tone")

    port, devtools = free_port(), free_port()
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", port), functools.partial(QuietHandler, directory=str(REPO)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/index.html?{url_query}"
    chrome = subprocess.Popen(
        [args.chrome, "--headless=new", f"--remote-debugging-port={devtools}",
         f"--user-data-dir={scratch / 'profile'}", "--no-first-run", "--no-default-browser-check",
         "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", *flags,
         "--autoplay-policy=no-user-gesture-required", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    title, t0 = "", time.perf_counter()
    try:
        while not title and time.perf_counter() - t0 < args.timeout:
            time.sleep(0.5)
            title = page_title(devtools)
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=10)
        except subprocess.TimeoutExpired:
            chrome.kill()
        server.shutdown()
        shutil.rmtree(scratch, ignore_errors=True)

    if not title:
        print(f"FAIL     no result in the page title within {args.timeout:.0f} s")
        return 1
    passed = title.startswith("SELFTEST PASS")
    print(f"{'PASS' if passed else 'FAIL':8s} {title}  ({time.perf_counter() - t0:.1f} s)")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
