#!/usr/bin/env python3
# synth_corpus.py - a synthetic corpus for the parity test in CI (M5).
#
# The Testing corpus is real voice takes and stays on Rick's machine (Rick 2026-10-22:
# CI's parity runs on a synthetic set; the real corpus is never committed). This writes
# folders shaped like it, one per room, each holding a phrase, a room-tone and a clap
# take (<id>_en_s_phrase.wav and so on, 48 kHz, 24-bit mono), seeded so every run writes
# the same files. Parity asks whether the cores score a take the same in Pyodide as in
# CPython, which any take can answer; what a synthetic set cannot show is how real rooms
# and voices score, so the real corpus stays the release check.
#
# The rooms are built to reach every branch the page's flow takes under Platform Band
# (noise REJECT below 20 dB SNR, WARN below 48, CAUTION below 55; RT60 pass below
# 0.25 s, production below 0.55, borderline below 0.90; 3 usable claps at least):
#
#   91001  level PASS, SNR 60 (PASS), RT60 0.20 s (pass): the whole flow runs
#   91002  SNR 51 (CAUTION), RT60 0.40 s (production)
#   91003  SNR 42 (WARN): the page skips room reflection (the lo-fi band); the
#          noise core's Default (reject below 38) still passes it
#   91004  SNR 15 (REJECT): the page stops at the quiet-room gate
#   91005  the phrase 40 dB down (TOO_QUIET): the page stops at the level gate
#   91006  RT60 1.20 s: too live, a reject that more claps cannot help
#   91007  two claps: unmeasured, and the platform would ask for more
#
# The SNR is set with the cores' own A-weighted meter (the one both the level check and
# the quiet room use), so each room lands mid-band, not on an edge.
#
# "Speech" is a voiced-sounding stand-in: a 120 Hz harmonic series rolled off above
# 1 kHz, syllable-modulated at 4 Hz in phrases with pauses, over the room's own noise.
#
# Run:   python3 tests/parity/synth_corpus.py <out dir>     (the folder must not exist)
# Exit:  0 written, 2 not run (the folder exists)
import sys
sys.dont_write_bytecode = True   # leave no __pycache__ in py/

from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import lfilter

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "py"))
from cores.level_check_phrase_core import measure_aweighted_rms_dba  # noqa: E402

RATE = 48000
# id: (SNR dB, RT60 s, claps, phrase gain dB)
ROOMS = {
    "91001": (60.0, 0.20, 6, 0.0),
    "91002": (51.0, 0.40, 6, 0.0),
    "91003": (42.0, 0.70, 6, 0.0),
    "91004": (15.0, 0.30, 6, 0.0),
    "91005": (60.0, 0.20, 6, -40.0),
    "91006": (60.0, 1.20, 6, 0.0),
    "91007": (60.0, 0.30, 2, 0.0),
}


def speech(rng, seconds=6.0):
    """The phrase stand-in, peaking at -6 dBFS."""
    t = np.arange(int(RATE * seconds)) / RATE
    voiced = sum(np.sin(2 * np.pi * 120 * k * t + rng.uniform(0, 2 * np.pi)) / max(1.0, k * 120 / 1000)
                 for k in range(1, 34))
    syllables = np.clip(np.sin(2 * np.pi * 4 * t), 0, None) ** 2
    phrases = ((t % 2.0) < 1.6).astype(float)   # 1.6 s phrases, 0.4 s pauses
    x = voiced * syllables * phrases
    return x * 10 ** (-6 / 20) / np.max(np.abs(x))


def room_noise(rng, n, level_dba):
    """Room noise, white through a one-pole low-pass at about 1.5 kHz, at an A-weighted level."""
    x = lfilter([0.18], [1.0, -0.82], rng.standard_normal(n))
    return x * 10 ** ((level_dba - measure_aweighted_rms_dba(x, RATE)) / 20)


def claps(rng, rt60, count, floor):
    """Claps 2 s apart over the room's noise: each a noise burst peaking at -8 dBFS that
    decays 60 dB in rt60 seconds."""
    x = floor[:int(RATE * (1.5 + 2.0 * max(count, 3)))].copy()
    t = np.arange(int(RATE * min(2.0, max(1.5, rt60 * 2.5)))) / RATE
    for k in range(count):
        burst = rng.standard_normal(len(t)) * np.exp(-6.91 * t / rt60)
        start = int(RATE * (0.5 + 2.0 * k))
        x[start:start + len(t)] += burst * 10 ** (-8 / 20) / np.max(np.abs(burst))
    return x


def write_room(out, room, snr, rt60, count, gain_db):
    rng = np.random.default_rng(int(room))
    voice = speech(rng)
    # The room is set against the voice at full level; 91005's phrase is then turned down.
    floor = room_noise(rng, RATE * 16, measure_aweighted_rms_dba(voice, RATE) - snr)
    phrase = voice * 10 ** (gain_db / 20)
    folder = out / f"synthetic_{room}"
    folder.mkdir(parents=True)
    takes = {"phrase": phrase + floor[:len(phrase)],
             "noise": floor[RATE:RATE * 9],   # 8 s of room tone
             "clap": claps(rng, rt60, count, floor)}
    for role, x in takes.items():
        sf.write(str(folder / f"{room}_en_s_{role}.wav"), x, RATE, subtype="PCM_24")
    return folder


def main():
    if len(sys.argv) != 2:
        print("usage: python3 tests/parity/synth_corpus.py <out dir>")
        return 2
    out = Path(sys.argv[1])
    if out.exists():
        print(f"NOT RUN  {out} exists; give a new folder")
        return 2
    for room, spec in ROOMS.items():
        write_room(out, room, *spec)
    print(f"wrote    {len(ROOMS)} synthetic rooms to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
