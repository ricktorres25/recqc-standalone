#!/usr/bin/env python3
# verdict_length.py - does a 5 s take get the same format verdict as a longer take?
#
# Rick ruled 2026-10-21 that RecQC runs verify_samples on the 5 s level phrase (design
# note section 4.2), where the platform runs it on the longer audition take. This checks
# the premise: the verdict on each 5 s stretch of a long speech take against the verdict
# on the whole take. Same chain, same room, same session; only the length changes.
#
# With no arguments it reads the Testing corpus's phrase takes (22 to 56 s of speech
# each), then the same takes band-limited (48 kHz -> 44.1 -> 48, the classic 48 kHz file
# with nothing above 22.05 kHz), so both directions are tried: a native take must not
# lose its verdict in a short stretch, and a band-limited one must not gain one. Give
# WAV paths instead to check your own takes, for example a 20 s read saved with the
# page's ?debug download.
#
# Run:   python3 tests/verdict_length.py [take.wav ...]
# Exit:  0 every 5 s stretch agrees with its whole take, 1 one or more differ (a finding
#        to report, not a crash), 2 not run (no takes found)
import sys
sys.dont_write_bytecode = True   # leave no __pycache__ in py/

from pathlib import Path

import numpy as np
import soundfile as sf
import soxr

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "py"))
from cores import provenance  # noqa: E402  (importing the package runs the start check)

CORPUS = REPO.parent / "Screening Pipeline" / "Testing corpus"
WINDOW_SECONDS = 5.0


def read_take(path):
    """(samples, rate, lossless), read as verify_provenance reads a file: float32, with
    losslessness from the subtype."""
    info = sf.info(str(path))
    data, rate = sf.read(str(path), dtype="float32", always_2d=False)
    return data, rate, provenance.is_lossless(info.subtype)


def band_limited(data, rate):
    """The take through a 44.1 kHz chain and back up to 48 kHz with soxr (the platform's
    resampler), as 24-bit samples: a 48 kHz file with nothing above 22.05 kHz. Not scipy's
    resample_poly: its filter's transition band straddles 22.05 kHz, so its round trip
    keeps energy up there and still reads native."""
    y = soxr.resample(soxr.resample(data.astype(np.float64), rate, 44100), 44100, rate)
    y = np.clip(y, -1.0, 1.0 - 2.0 ** -23)
    return (np.round(y * 2 ** 23) / 2 ** 23).astype(np.float32)


def verdicts(data, rate, lossless):
    """(verdict on the whole take, [verdict on each 5 s stretch, in order]), each from
    the same verify_samples call verify_provenance makes."""
    whole = provenance.verify_samples(data, rate, lossless).verdict
    step = int(WINDOW_SECONDS * rate)
    cuts = [provenance.verify_samples(data[i:i + step], rate, lossless).verdict
            for i in range(0, len(data) - step + 1, step)]
    return whole, cuts


def main():
    own = [Path(a) for a in sys.argv[1:]]
    takes = own or sorted(CORPUS.glob("*/*_phrase.wav"))
    if not takes:
        print(f"NOT RUN  no takes given and none found in {CORPUS}")
        return 2

    # (label, samples, rate, lossless) for every take to try.
    runs = []
    for take in takes:
        data, rate, lossless = read_take(take)
        runs.append((str(take) if own else f"{take.parent.name}/{take.name}", data, rate, lossless))
    if not own:
        runs += [(f"{label} band-limited", band_limited(data, rate), rate, lossless)
                 for label, data, rate, lossless in list(runs) if rate == 48000]

    total = differ = 0
    for label, data, rate, lossless in runs:
        whole, cuts = verdicts(data, rate, lossless)
        bad = [i for i, v in enumerate(cuts) if v != whole]
        total += len(cuts)
        differ += len(bad)
        print(f"{'PASS' if not bad else 'DIFF':8s} {label}: {len(data) / rate:.1f} s, whole take "
              f"{whole}; {len(cuts) - len(bad)} of {len(cuts)} 5 s stretches agree")
        for i in bad:
            print(f"         {i * WINDOW_SECONDS:.0f}-{(i + 1) * WINDOW_SECONDS:.0f} s: {cuts[i]}")

    print()
    if differ:
        print(f"RESULT: {differ} of {total} 5 s stretches got a different verdict from their whole take")
        return 1
    print(f"RESULT: all {total} 5 s stretches of {len(runs)} takes got their whole take's verdict")
    return 0


if __name__ == "__main__":
    sys.exit(main())
