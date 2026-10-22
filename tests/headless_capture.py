#!/usr/bin/env python3
# headless_capture.py - the whole M2 page path in headless Chrome, with a fake mic.
#
# Serves this repo on a local port and opens index.html?selftest in headless Chrome
# with Chrome's fake media device (a synthetic tone in place of a mic; the mic
# permission is granted without a prompt). The selftest turns the mic on, records 3 s
# through the platform's meter worklet, encodes the WAV with the platform's encoder,
# scores it in the Pyodide module worker, and writes SELFTEST PASS or SELFTEST FAIL into
# the page title. This script reads the title from Chrome's DevTools HTTP endpoint
# (/json/list), so it needs nothing beyond the standard library.
#
# What a pass shows: the module worker boots, the lifted capture code runs in a real
# browser, and a captured take comes back scored by the cores. Chrome's fake mic reports
# exactly 44100 Hz, so this run takes the 44.1 kHz path (soxr working copy) every time.
# What it cannot show: the 48 kHz path, a real mic, a real room, or how Chrome treats
# your interface; those are checked by hand.
#
# Run:   python3 tests/headless_capture.py [--chrome <path>] [--timeout 120]
# Exit:  0 pass, 1 fail, 2 not run (Chrome not found)
import argparse
import functools
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
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


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
            return t["title"]
    return ""


def main():
    ap = argparse.ArgumentParser(description="M2 page path in headless Chrome with a fake mic")
    ap.add_argument("--chrome", default=CHROME)
    ap.add_argument("--timeout", type=float, default=120.0, help="seconds to wait for a result")
    args = ap.parse_args()
    if not Path(args.chrome).exists():
        print(f"NOT RUN  Chrome not found at {args.chrome}")
        return 2

    port, devtools = free_port(), free_port()
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", port), functools.partial(QuietHandler, directory=str(REPO)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    profile = tempfile.mkdtemp(prefix="recqc-chrome-")
    url = f"http://127.0.0.1:{port}/index.html?selftest"
    chrome = subprocess.Popen(
        [args.chrome, "--headless=new", f"--remote-debugging-port={devtools}",
         f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
         "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
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
        shutil.rmtree(profile, ignore_errors=True)

    if not title:
        print(f"FAIL     no result in the page title within {args.timeout:.0f} s")
        return 1
    passed = title.startswith("SELFTEST PASS")
    print(f"{'PASS' if passed else 'FAIL':8s} {title}  ({time.perf_counter() - t0:.1f} s)")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
