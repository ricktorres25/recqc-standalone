"""Spectral provenance verifier — proves a recording is native ≥48k, it does NOT
"detect fakes" (design sketch decision 6).

Pure DSP (soundfile + numpy FFT), no DB / FastAPI deps, so it is importable and
unit-testable in isolation and could later move to a standalone core. Three-way
verdict keyed on the resampling fingerprint and supra-Nyquist energy, never on
vague rolloff:

  - confirmed_native    real signal/noise energy ABOVE 22.05 kHz. Physics: a 44.1k
                        source resampled to 48k cannot create energy above its
                        22.05 kHz Nyquist, so its presence is near-proof of a true
                        ≥48k capture. Requires a LOSSLESS source (decision 7).
  - confirmed_resampled a hard spectral brickwall at a source-rate Nyquist below the
                        true Nyquist (22.05k for a 44.1 source, 16k for 32k, 11.025k
                        for 22.05k) — the fingerprint resampling leaves behind.
  - indeterminate       everything else, and deliberately the catch-all: a genuine but
                        dull 48k mic that rolls off early; an NS/codec hard-cap (LPF
                        cliff); and — since prov-2026-07-21 — ANY sharp bandwidth wall
                        below the true Nyquist that sits at no KNOWN source rate, i.e. a
                        band-limited capture whose ceiling we cannot attribute.
                        Conservative; never branded resampled.

NOTE on the native test (worth stating because the wording above invites a misreading):
confirmed_native is decided by a RELATIVE continuity comparison across 22.05 kHz, not by an
absolute measurement of supra-Nyquist energy. The absolute term (`supra_over_floor_db`) is
computed for the audit trail but is NOT decisive — 06-27b demoted it because real 48k SPEECH
rolls off ~40 dB by 20k and was being mis-failed. That relative test is why the gates above it
matter: when a capture is DEAD above a wall, both compared bands are noise floor and
"dead == dead" reads as smooth decay. Restoring an absolute threshold does NOT fix this and
was measured to be strictly worse (dull-but-genuine mics score further below the floor than
band-limited fakes do), so the wall/cliff gates carry that load instead.

The asymmetry is the point: confirmed_native is an earned, evidence-backed label;
the system never claims hi-fi it cannot prove, and never falsely accuses a real
recording.

LOSSLESSNESS RULE (decision 7), built now but DORMANT on the live system: behind
the two-part WAV door-gate (cores.ingest_capture) the verifier only ever sees
WAV/PCM (unambiguously lossless), so the live path always takes the lossless branch
and the lossy→cap branch is unreachable by construction. It is exercised only by the
unit tests + the deferred external bulk-upload endpoint. Same pattern as the dormant
channel-0 fallback in the analysis cores.

Thresholds are PROVISIONAL (versioned below); `validate_against_corpus` is the
calibration harness, same pattern as the noise scanner's provisional reject_excess_db
→ transient_scan. Rick generates the asset set (sketch "Test asset list").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import soundfile as sf

# --------------------------------------------------------------------------- #
# Provisional thresholds (versioned — bump on any constant change so a stamped
# verdict records which calibration produced it).
# --------------------------------------------------------------------------- #
THRESHOLDS_VERSION = "prov-2026-10-10"  # 10-10: the wall scan reaches below 8 kHz
# (PROVENANCE_WALL_LEAK_DESIGN_SKETCH.md §10, ruled by Rick). _find_brickwall used to start at a
# hard-coded 8000 Hz, so a band limit at 7 kHz or below with a decaying tail (narrowband
# Bluetooth is a 4 kHz cut) stamped no wall, missed the cliff gate and read confirmed_native.
# It now scans from wall_scan_floor_hz (3 kHz), and below wall_floor_prox_min_hz (8 kHz) a 35 dB
# drop is a wall without the near-the-floor test, which a low wall can never pass: the floor (the
# 10th percentile of 1-10 kHz) lies deep in its own decaying tail. Measured first: no verdict
# changed on 500 box captures (438 native) or 3,130 local ones; genuine audio peaked 8.6 dB
# under the trigger. Old stamps keep prov-2026-07-21 (no backfill: nothing changes on them).
# 07-21: bandwidth-wall gate on confirmed_native
# (closes the wall-too-low-for-the-resample-list, too-shallow-for-the-cliff hole: ANY stamped
# sub-Nyquist brickwall now disqualifies native, not only one at a KNOWN source Nyquist. Adds
# no constant — the version bump is so a stamped verdict stays attributable to the calibration
# that produced it, since pre- and post-fix rows are otherwise indistinguishable. See the
# BANDWIDTH-WALL GATE block in classify() for the measured rationale.
# 06-28b: LPF-cliff gate on confirmed_native
# (closes the false-native hole on NS/codec headsets. The DISTINGUISHING feature of a
# noise-cancelling/codec capture is a HARD LOW-PASS CLIFF — a sharp drop into the noise
# floor (the NS hard-caps the top to mask accumulated noise; on a spectrogram it is a
# dead-flat horizontal line). A genuine capture — even a DULL one — decays SMOOTHLY and
# its HF stays above the floor; it has no cliff. So we detect the cliff, not the bandwidth.
# This preserves the dull-but-genuine-mic = native design case for free and doubles as the
# DSP-headset signal for lo-fi-salvage routing (detail.dsp_lpf_wall_hz). See _lpf_cliff.
# Calibrated against a matched controlled corpus (clean studio / Ocean-Way-modelled dull /
# NS-headset, all captured through the platform). 06-28a (bandwidth-energy gate) was
# REVERTED: cumulative energy measures where speech energy SITS (intrinsically low-freq),
# not the capture-bandwidth ceiling, so it demoted clean studio speech too.
# 06-27b: continuity-drop native test (real-48k calibration).

DEFAULT_THRESHOLDS = {
    # CONFIRMED-NATIVE test (recalibrated 2026-06-27 against real 48k captures): the
    # energy must be CONTINUOUS across 22.05 kHz — the drop from [18-22k] to the supra
    # band [22.05-23.5k] must be no more than this. A genuine ≥48k capture decays
    # smoothly into its noise floor (real captures measured 2-8 dB drop); a 44.1→48
    # resample cliffs at its 22.05k Nyquist (tens of dB, caught as a wall first). This is
    # a LOCAL comparison of adjacent bands, so it is immune to a signal's overall spectral
    # tilt — the fix for real 48k SPEECH reading 'indeterminate' (speech rolls off ~40 dB
    # by 20k, so the old supra-vs-1-10k-floor test mis-failed it). PROVISIONAL.
    "continuity_drop_db": 20.0,
    # LPF-CLIFF gate on confirmed_native (2026-06-28b). A noise-cancelling / codec headset
    # hard-caps the top end: a SHARP drop into the noise floor (a dead-flat line on the
    # spectrogram). _lpf_cliff scans below the native band for a sustained drop of at least
    # cliff_drop_db where the post-wall band lands within cliff_floor_prox_db of the
    # spectrum's TOP-of-band noise floor. A genuine capture (incl. a DULL one) decays
    # SMOOTHLY and its HF stays above the floor, so it has NO cliff → not gated. Calibrated
    # on the controlled matched corpus: NS-headset cliffs measure 39-52 dB into a -70 dB
    # floor at ~7.8k; clean studio and Ocean-Way-modelled DULL show max adjacent drops of
    # ~12-15 dB and never land on the floor below Nyquist → no cliff. 30 dB sits well
    # between (headset margin ~9 dB, smooth-decay margin ~15 dB). A detected cliff caps the
    # ceiling (verdict→indeterminate) AND is stamped (detail.dsp_lpf_wall_hz) as the
    # DSP-headset signal a reviewer routes to lo-fi salvage. PROVISIONAL.
    "cliff_drop_db": 30.0,            # min sharp drop (below→above) to count as an LPF cliff
    "cliff_floor_prox_db": 12.0,      # post-wall band must sit within this of the top floor
    "cliff_scan_hz": (3000.0, 12000.0),  # search band: telephone (~3.4k) .. wideband (~8k)
    "cliff_pre_span_hz": 2000.0,      # pre-wall averaging window
    "cliff_gap_hz": 500.0,            # skip the transition region right at the wall
    "cliff_post_span_hz": 2000.0,     # post-wall averaging window
    "cliff_floor_top_hz": 3000.0,     # top-of-spectrum band (Nyquist-this .. Nyquist) = floor
    # a brickwall is a drop of at least this many dB across the wall (below→above).
    "wall_drop_db": 35.0,
    # where the wall scan starts (2026-10-10; was a hard-coded 8000): the cliff scan's low
    # edge, below the telephone band (~3.4 kHz) and the 4 kHz cut of an 8 kHz source.
    "wall_scan_floor_hz": 3000.0,
    # at and above this frequency a wall must also land near the floor (the post-wall band
    # within 6 dB of floor_db); below it the drop alone decides, since a low wall's decaying
    # tail always sits well above a 1-10 kHz floor measured inside that same tail.
    "wall_floor_prox_min_hz": 8000.0,
    # a detected wall counts as "at" a candidate source-Nyquist within this tolerance.
    "wall_tol_hz": 900.0,
    # known source Nyquists a resample-to-48k leaves a wall at.
    "source_nyquists_hz": (22050.0, 16000.0, 11025.0),
    # spectrum analysis
    "nfft": 8192,
    "midband_hz": (1000.0, 6000.0),   # reference band for the spectrum
    "floor_percentile": 10.0,         # noise-floor estimate within the floor band
    # The noise floor is estimated over a GUARANTEED-LIVE low-mid band, below the
    # lowest resample wall of interest (11.025k). Estimating it up to Nyquist would
    # let a large dead band (e.g. a 32k→48k upsample, dead above 16k) drag the floor
    # into the dead region and falsely read the supra band as "present".
    "floor_band_hz": (1000.0, 10000.0),
}

# soundfile subtypes that are lossless (PCM family + the lossless codecs). Anything
# else (MP3/Vorbis/AAC via libsndfile/ffmpeg) is lossy → cannot be confirmed_native.
_LOSSLESS_SUBTYPES = {
    "PCM_S8", "PCM_U8", "PCM_16", "PCM_24", "PCM_32",
    "FLOAT", "DOUBLE", "ALAC_16", "ALAC_20", "ALAC_24", "ALAC_32",
}


@dataclass
class ProvenanceResult:
    verdict: str                 # confirmed_native | confirmed_resampled | indeterminate
    confidence: str              # high | medium | low
    lossless: bool
    detail: dict = field(default_factory=dict)
    thresholds_version: str = THRESHOLDS_VERSION

    def as_dict(self) -> dict:
        return {
            "spectral_verdict": self.verdict,
            "confidence": self.confidence,
            "lossless": self.lossless,
            "thresholds_version": self.thresholds_version,
            "detail": self.detail,
        }


def is_lossless(subtype: str | None) -> bool:
    """True if a soundfile subtype is lossless. Unknown/None → treated as lossy
    (fail-safe: an unrecognized codec cannot earn the confirmed_native label)."""
    return bool(subtype) and subtype.upper() in _LOSSLESS_SUBTYPES


def _strongest_channel(data: np.ndarray) -> np.ndarray:
    """Mono view of the analysis signal: the strongest channel by RMS (matches the
    working-copy deriver; avoids the Chromium stereo-silent-R downmix trap)."""
    if data.ndim == 1:
        return data
    rms = np.sqrt((data.astype(np.float64) ** 2).mean(axis=0))
    return data[:, int(np.argmax(rms))]


def _avg_power_spectrum(mono: np.ndarray, sr: int, nfft: int):
    """Hann-windowed, overlap-averaged power spectrum in dB. Short clips fall back
    to a single zero-padded frame. Returns (power_db, freqs_hz)."""
    x = np.asarray(mono, dtype=np.float64)
    if x.size < nfft:
        frames = [np.pad(x, (0, nfft - x.size))]
    else:
        hop = nfft // 2
        starts = range(0, x.size - nfft + 1, hop)
        frames = [x[s:s + nfft] for s in starts]
    win = np.hanning(nfft)
    acc = np.zeros(nfft // 2 + 1, dtype=np.float64)
    for fr in frames:
        spec = np.fft.rfft(fr * win)
        acc += (spec.real ** 2 + spec.imag ** 2)
    acc /= max(len(frames), 1)
    power_db = 10.0 * np.log10(acc + 1e-20)
    freqs = np.fft.rfftfreq(nfft, d=1.0 / sr)
    return power_db, freqs


def _band_mean_db(power_db, freqs, lo, hi) -> float | None:
    mask = (freqs >= lo) & (freqs < hi)
    if not mask.any():
        return None
    return float(power_db[mask].mean())


def _find_brickwall(power_db, freqs, floor_db, thr):
    """Largest sustained drop in the spectrum: the frequency where power falls by
    ≥wall_drop_db into the floor and stays there. Returns (wall_hz, drop_db) or
    (None, 0.0). Scans from wall_scan_floor_hz up to just below the true Nyquist; below
    wall_floor_prox_min_hz the drop alone qualifies a wall (see THRESHOLDS_VERSION 10-10)."""
    # Smooth lightly to ignore single-bin spikes.
    k = 5
    kernel = np.ones(k) / k
    smooth = np.convolve(power_db, kernel, mode="same")
    best_hz, best_drop = None, 0.0
    # compare each frequency's local pre-band vs post-band power.
    span_hz = 1500.0
    df = freqs[1] - freqs[0] if len(freqs) > 1 else 1.0
    span = max(int(span_hz / df), 1)
    for i in range(span, len(freqs) - span):
        f = freqs[i]
        if f < thr["wall_scan_floor_hz"]:
            continue
        below = smooth[i - span:i].mean()
        above = smooth[i:i + span].mean()
        drop = below - above
        # a real wall: big drop AND the post-band sits near the noise floor, except below
        # wall_floor_prox_min_hz, where no analog roll-off reaches a 35 dB step over 1.5 kHz
        # (about 47 dB/octave at 3 kHz) and a low wall's tail never reaches that floor.
        near_floor = above <= floor_db + 6.0 or f < thr["wall_floor_prox_min_hz"]
        if drop >= thr["wall_drop_db"] and near_floor and drop > best_drop:
            best_drop, best_hz = drop, float(f)
    return best_hz, best_drop


def _lpf_cliff(power_db, freqs, thr):
    """Detect a hard low-pass CLIFF — the noise-cancelling/codec hard-cap signature: a
    sharp sustained drop into the noise floor below the native band. The noise floor is
    taken from the TOP of the spectrum (guaranteed post-content for any LPF wall). Scans
    the cliff band comparing a pre-wall window to a post-wall window (a gap skips the
    transition); a cliff is a drop ≥ cliff_drop_db whose post-window lands within
    cliff_floor_prox_db of that floor. A genuine capture — even a dull one — decays
    smoothly and its HF stays above the floor, so no window pair both drops hard AND lands
    on the floor. Returns (wall_hz, drop_db) of the largest qualifying cliff, else
    (None, 0.0)."""
    nyq = freqs[-1]
    top = (freqs >= nyq - thr["cliff_floor_top_hz"]) & (freqs <= nyq - 200.0)
    floor = float(np.median(power_db[top])) if top.any() else float(power_db.min())
    k = 5
    smooth = np.convolve(power_db, np.ones(k) / k, mode="same")
    lo, hi = thr["cliff_scan_hz"]
    pre_span, gap, post_span = (thr["cliff_pre_span_hz"], thr["cliff_gap_hz"],
                                thr["cliff_post_span_hz"])
    best_hz, best_drop = None, 0.0
    scan = (freqs >= lo) & (freqs <= hi)
    for f in freqs[scan]:
        pre = (freqs >= f - pre_span) & (freqs < f)
        post = (freqs >= f + gap) & (freqs < f + gap + post_span)
        if not pre.any() or not post.any():
            continue
        prev, postv = float(smooth[pre].mean()), float(smooth[post].mean())
        drop = prev - postv
        if (drop >= thr["cliff_drop_db"] and postv <= floor + thr["cliff_floor_prox_db"]
                and drop > best_drop):
            best_hz, best_drop = float(f), drop
    return best_hz, best_drop


def classify(power_db, freqs, sr: int, lossless: bool,
             thresholds: dict | None = None) -> ProvenanceResult:
    """Pure classifier over a precomputed spectrum — the unit-test seam (lets the
    DORMANT lossy branch be exercised without synthesizing a lossy file)."""
    thr = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    nyq = sr / 2.0
    detail: dict = {"sr": sr, "nyquist_hz": nyq}

    # Noise-floor estimate over a guaranteed-live low-mid band (below the lowest
    # resample wall), so a large dead band can't drag the floor down (see threshold).
    flo, fhi = thr["floor_band_hz"]
    fband = (freqs >= flo) & (freqs < min(fhi, nyq - 500.0))
    floor_db = (float(np.percentile(power_db[fband], thr["floor_percentile"]))
                if fband.any() else float(power_db.min()))
    detail["floor_db"] = round(floor_db, 2)

    wall_hz, wall_drop = _find_brickwall(power_db, freqs, floor_db, thr)
    detail["wall_hz"] = round(wall_hz, 1) if wall_hz else None
    detail["wall_drop_db"] = round(wall_drop, 2)

    # Hard low-pass cliff (NS/codec headset hard-cap). Stamped always so downstream
    # tier/disposition logic can route a flagged capture to lo-fi salvage on the number,
    # not just the verdict.
    cliff_hz, cliff_drop = _lpf_cliff(power_db, freqs, thr)
    detail["dsp_lpf_wall_hz"] = round(cliff_hz, 0) if cliff_hz else None
    detail["dsp_lpf_drop_db"] = round(cliff_drop, 1)

    # Local continuity across 22.05 kHz (needs a true Nyquist above it to even look):
    # the drop from the band just below to the supra band. Small = smooth decay (native);
    # large = a cliff (resample). Adjacent-band comparison → tilt-independent.
    continuity_drop = None
    if nyq > 22550.0:
        pre = _band_mean_db(power_db, freqs, 18000.0, 22000.0)
        post = _band_mean_db(power_db, freqs, 22050.0, min(nyq - 300.0, 23500.0))
        if pre is not None and post is not None:
            continuity_drop = pre - post
            detail["continuity_drop_db"] = round(continuity_drop, 2)
        # supra-vs-floor kept for the audit trail only (no longer decisive).
        above = _band_mean_db(power_db, freqs, 22550.0, min(nyq - 300.0, 24000.0))
        if above is not None:
            detail["supra_over_floor_db"] = round(above - floor_db, 2)

    # DECISION 7 (dormant live): a lossy source can never be confirmed_native; the
    # codec lowpass both mimics and masks the resampling fingerprint.
    if not lossless:
        detail["reason"] = "lossy source — capped, provenance unreliable"
        return ProvenanceResult("indeterminate", "low", False, detail)

    # RESAMPLE FIRST: a sharp wall at a source Nyquist below the true Nyquist. Checked
    # before the native test so a low-rate upsample (e.g. 32k→48, dead AND continuous
    # above 16k) is caught here, not mistaken for smooth native decay.
    if wall_hz is not None:
        for cand in thr["source_nyquists_hz"]:
            if cand < nyq - 1000.0 and abs(wall_hz - cand) <= thr["wall_tol_hz"]:
                detail["reason"] = f"brickwall at ~{cand:.0f} Hz (source Nyquist)"
                detail["source_nyquist_hz"] = cand
                return ProvenanceResult("confirmed_resampled", "high", True, detail)

    # LPF-CLIFF GATE: a hard low-pass cliff into the noise floor is the NS/codec headset
    # hard-cap signature. It must disqualify native BEFORE the continuity test, because a
    # hard-capped capture is DEAD above its wall (incl. across 22.05 kHz), so continuity
    # reads ~0 ("dead == dead") and would otherwise read native. A genuine capture (incl.
    # a dull one) has no cliff, so this never fires on it. Caps the ceiling and stamps the
    # wall as the DSP-headset signal for lo-fi-salvage routing.
    if cliff_hz is not None:
        detail["reason"] = (f"hard low-pass cliff at ~{cliff_hz:.0f} Hz ({cliff_drop:.0f} dB "
                            f"into the floor) — NS/codec capture, cannot confirm native")
        return ProvenanceResult("indeterminate", "high", True, detail)

    # BANDWIDTH-WALL GATE (2026-07-21). ANY detected brickwall below the true Nyquist
    # disqualifies confirmed_native — not merely a wall at a KNOWN source Nyquist. Reaching
    # here means _find_brickwall stamped a sustained ≥wall_drop_db drop (into the floor at and
    # above 8 kHz; any such drop below it, since 2026-10-10) that matched no candidate source
    # rate: a band-limited capture whose ceiling we cannot attribute. Like the LPF-cliff case it is DEAD above the wall (incl. across 22.05 kHz),
    # so the continuity test below would read "dead == dead" and brand it native.
    #
    # WHY THE CLIFF GATE ABOVE DOES NOT ALREADY CATCH THIS: the two detectors use DIFFERENT
    # noise floors — _find_brickwall compares against the 10th percentile of the live
    # 1-10 kHz band, _lpf_cliff against the median of the TOP 3 kHz of the spectrum. A wall
    # whose out-of-band residue keeps DECAYING (resampler reconstruction ripple, codec
    # residue, an upsampled narrowband route) sits near the low-mid floor but well ABOVE the
    # top-of-spectrum floor, so it never satisfies the cliff's "lands on the floor" test.
    # Measured: the leak is governed by tail TILT, not level — at 0 dB/oct the cliff fires at
    # every level from -70 to -110 dB; at ≥12 dB/oct it fires at none. So this cannot be
    # fixed by moving cliff_drop_db / cliff_floor_prox_db; the cliff detects FLATNESS, this
    # gate detects the WALL. Live corpus: one asset (a 48k capture walled at 8003.9 Hz,
    # 39.1 dB) was stamped confirmed_native/high while two sibling captures at the same wall
    # frequency were correctly held at indeterminate — differing only in tail shape.
    if wall_hz is not None:
        detail["reason"] = (f"bandwidth wall at ~{wall_hz:.0f} Hz ({wall_drop:.0f} dB), "
                            f"not at a known source Nyquist — cannot confirm native")
        return ProvenanceResult("indeterminate", "high", True, detail)

    # CONFIRMED NATIVE: energy decays smoothly across 22.05 kHz (no resample cliff) and —
    # having passed the LPF-cliff gate above — has no hard-cap. A resample without a
    # detected wall fails this (large continuity drop) and falls through to indeterminate.
    if continuity_drop is not None and continuity_drop <= thr["continuity_drop_db"]:
        detail["reason"] = "energy continuous across 22.05 kHz (no resample/LPF cliff)"
        return ProvenanceResult("confirmed_native", "high", True, detail)

    detail["reason"] = ("no source-Nyquist wall, but energy not continuous above "
                        "22.05 kHz" if nyq > 22550.0 else "no supra-Nyquist band (rate ≤ 44.1)")
    return ProvenanceResult("indeterminate", "medium", True, detail)


def verify_provenance(path: str | Path,
                      thresholds: dict | None = None) -> ProvenanceResult:
    """Read a file and return its spectral provenance verdict. Reads the strongest
    channel at the file's TRUE rate (the master, pre-resample)."""
    info = sf.info(str(path))
    data, sr = sf.read(str(path), dtype="float32", always_2d=False)
    return verify_samples(data, sr, is_lossless(info.subtype), thresholds)


def verify_samples(data: np.ndarray, sr: int, lossless: bool,
                   thresholds: dict | None = None) -> ProvenanceResult:
    """verify_provenance on samples already in memory (float32, as it reads them), for
    audio that has no file of its own: the editing-return report stamps the lane an
    editor was given, which for a scripted conversation is its turns as packed
    (SPEECH_QUALITY_PREDICTORS_DESIGN_SKETCH.md §10.5, Rick 2026-10-10)."""
    thr = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    mono = _strongest_channel(data)
    power_db, freqs = _avg_power_spectrum(mono, sr, thr["nfft"])
    return classify(power_db, freqs, sr, lossless, thr)


# --------------------------------------------------------------------------- #
# Format → audio_tier CEILING (design sketch decision 3 + approval resolution 1).
# audio_tier is an ordered Integer 1..4 (tiers.AUDIO_TIERS). The ceiling is an UPPER
# clamp on that axis: only a confirmed-native LOSSLESS capture may reach 4 (Hi-fi);
# everything else (indeterminate / resampled / any lossy) caps at 3 ("production").
# Below 44.1 never reaches here (door-rejected pre-save).
# --------------------------------------------------------------------------- #
CEILING_NATIVE = 4
CEILING_DEFAULT = 3


def ceiling_from_provenance(provenance: dict | None) -> int:
    """The upper bound on the assignable audio_tier given an asset's provenance blob.
    Fail-safe: missing/unparseable provenance → the conservative production ceiling."""
    if not provenance:
        return CEILING_DEFAULT
    if provenance.get("spectral_verdict") == "confirmed_native" and provenance.get("lossless"):
        return CEILING_NATIVE
    return CEILING_DEFAULT


# --------------------------------------------------------------------------- #
# Calibration harness — provisional-then-calibrate. Run over a labelled corpus to
# tune supra_over_floor_db / wall_drop_db / wall_tol_hz. Same role as the noise
# scanner's transient_scan against its labelled reference corpus.
#   labelled = [(path, expected_verdict), ...]
# --------------------------------------------------------------------------- #
def validate_against_corpus(labelled, thresholds: dict | None = None) -> dict:
    rows, correct = [], 0
    for path, expected in labelled:
        try:
            res = verify_provenance(path, thresholds)
            ok = (res.verdict == expected)
            correct += int(ok)
            rows.append({"path": str(path), "expected": expected,
                         "verdict": res.verdict, "confidence": res.confidence,
                         "ok": ok, "detail": res.detail})
        except Exception as e:  # noqa: BLE001 — a harness, report don't raise
            rows.append({"path": str(path), "expected": expected,
                         "verdict": "ERROR", "error": f"{type(e).__name__}: {e}",
                         "ok": False})
    n = len(rows)
    return {"n": n, "correct": correct, "accuracy": (correct / n if n else 0.0),
            "thresholds_version": THRESHOLDS_VERSION, "rows": rows}
