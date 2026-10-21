# rt60_analyzer_core_v5.py — RT60 Analyzer v0.5
#
# v0.5 ships the v0.4 algorithm unchanged (verified identical to
# rt60_analyzer_core_v4.py apart from this header and SCANNER_VERSION),
# promoted from the dev checkpoint to a shipped entry point so the
# screening-pipeline orchestration layer can wire to a stable v5 core.
#
# Deferred v0.5+ investigations (not yet implemented; carried over from the
# dev checkpoint, do not assume any are done):
#   1. Hallway sample-composition spot-check
#   2. Treated residual bias investigation
#   3. Hallway/Live high-RT60 underestimation (only relevant if the gate
#      threshold tightens below ~0.55s)

import hashlib
import json
import sys
import numpy as np
import yaml
from pathlib import Path
from datetime import datetime, timezone
from scipy.signal import butter, sosfilt, find_peaks
from scipy.stats import linregress

try:
    import soundfile as sf
    SF_AVAILABLE = True
except ImportError:
    SF_AVAILABLE = False

try:
    from pydub import AudioSegment
    PYDUB_AVAILABLE = True
except ImportError:
    PYDUB_AVAILABLE = False

# ---------------------------------------------------------------------------
# Algorithm internals — hardcoded, not exposed in YAML
# ---------------------------------------------------------------------------
_BAND_CENTERS_HZ          = [500, 1000, 2000]
_FILTER_ORDER             = 4
_DECAY_WINDOW_MAX_SEC     = 2.5
_POST_IMPULSE_BUFFER_SEC  = 0.1

# Lundeby constants
_LUNDEBY_NOISE_PERCENTILE  = 10    # use bottom 10% of late-decay samples as noise estimate
_LUNDEBY_NOISE_TAIL_FRAC   = 0.10  # fraction of total window used to estimate noise floor
_LUNDEBY_MARGIN_DB         = 10.0  # stop fitting this many dB above estimated noise floor
_LUNDEBY_MAX_ITER          = 5     # convergence iterations
_RT60_MAX_PLAUSIBLE_SEC    = 8.0   # results above this are noise-floor artifacts, not real rooms

# ---------------------------------------------------------------------------
# Scanner constants
# ---------------------------------------------------------------------------
_CLAP_PEAK_THRESHOLD_DBFS = -20.0
_MIN_INTER_CLAP_SEC       = 1.5
_CLIPPING_MAX_LINEAR      = 0.99
_ATTACK_MAX_MS            = 3.0
_CREST_MIN_DB             = 12.0
_ATTACK_LEVEL_DB          = -20.0

# ---------------------------------------------------------------------------
# Default config
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "profile_name": "Strict",
    "gate": {
        "reject_above":    0.50,   # matches configs/strict.yaml
        "pass_below":      0.25,   # matches configs/strict.yaml
        "min_valid_claps": 3,
    },
    "quality": {
        "fit_window":    "T20",
        "r_squared_min":  0.95,
    },
    "advanced": {
        "clap_peak_threshold_dbfs": _CLAP_PEAK_THRESHOLD_DBFS,
        "min_inter_clap_sec":       _MIN_INTER_CLAP_SEC,
        "clipping_max_linear":      _CLIPPING_MAX_LINEAR,
        "attack_max_ms":            _ATTACK_MAX_MS,
        "crest_min_db":             _CREST_MIN_DB,
    },
}

SCANNER_VERSION = "rt60_v0.5"

# Minimum RT60 the T20 method can reliably resolve. Rooms shorter than this
# produce fits that technically pass R² but span too few samples to be
# trustworthy — the fit window (~20dB of decay) only covers ~30ms at 0.09s RT60.
_MIN_RT60_RESOLVABLE_SEC = 0.10

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(config_path: Path = None) -> dict:
    config = _deep_copy_dict(DEFAULT_CONFIG)
    if config_path is None:
        return config
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")
    with open(config_path, "r") as f:
        user_config = yaml.safe_load(f)
    if not isinstance(user_config, dict):
        raise ValueError(f"Config must be a YAML mapping, got {type(user_config).__name__}")
    _merge_config(config, user_config)
    return config


def _deep_copy_dict(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out[k] = _deep_copy_dict(v)
        elif isinstance(v, list):
            out[k] = v[:]
        else:
            out[k] = v
    return out


def _merge_config(base: dict, override: dict):
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _merge_config(base[k], v)
        else:
            base[k] = v


def config_sha256(config: dict) -> str:
    """SHA-256 over the resolved config dict — the exact scoring policy as a hash
    rather than a mutable label, so a profile whose numbers were edited later is
    distinguishable from the original (audio band policy AB-6, Rick 2026-07-30).
    Deliberately identical in construction to
    noise_profile_scanner_core.config_sha256: JSON-safe, key-sorted, tight
    separators. The two must stay the same shape — a caller stamping both hashes
    should not have to care which core produced which."""
    serialized = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def get_builtin_config_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "configs"
    return Path(__file__).parent / "configs"


# ---------------------------------------------------------------------------
# Audio I/O
# ---------------------------------------------------------------------------

def load_audio(file_path: Path) -> tuple:
    errors = []
    if SF_AVAILABLE:
        try:
            data, sr = sf.read(str(file_path), dtype="float32")
            if data.ndim > 1:
                data = data[:, 0]
            return data, sr
        except Exception as e:
            errors.append(f"soundfile: {e}")
    if PYDUB_AVAILABLE:
        try:
            audio = AudioSegment.from_file(str(file_path)).set_channels(1)
            samples = np.array(audio.get_array_of_samples(), dtype=np.float32)
            sw = audio.sample_width
            if sw == 2:   samples /= 32768.0
            elif sw == 3: samples /= 8388608.0
            elif sw == 4: samples /= 2147483648.0
            return samples, audio.frame_rate
        except Exception as e:
            errors.append(f"pydub: {e}")
    if errors:
        raise RuntimeError(f"All audio backends failed: {'; '.join(errors)}")
    raise RuntimeError("No audio backend (soundfile or pydub required)")


# ---------------------------------------------------------------------------
# Band filtering
# ---------------------------------------------------------------------------

def _make_band_filter(center_hz: int, sr: int) -> np.ndarray:
    low  = center_hz / np.sqrt(2)
    high = min(center_hz * np.sqrt(2), sr / 2.0 * 0.95)
    return butter(_FILTER_ORDER, [low, high], btype="band", fs=sr, output="sos")


def _apply_band_filter(data: np.ndarray, sos: np.ndarray) -> np.ndarray:
    return sosfilt(sos, data)


# ---------------------------------------------------------------------------
# Clap detection
# ---------------------------------------------------------------------------

def detect_claps(data: np.ndarray, sr: int, config: dict) -> list:
    adv = config["advanced"]
    threshold_linear = 10 ** (adv["clap_peak_threshold_dbfs"] / 20)
    min_dist_samples = int(adv["min_inter_clap_sec"] * sr)
    envelope = np.abs(data)

    peaks_rough, _ = find_peaks(envelope, height=threshold_linear, distance=min_dist_samples)
    if len(peaks_rough) == 0:
        return []

    refine_win = int(0.010 * sr)
    refined = []
    for p in peaks_rough:
        lo = max(0, p - refine_win)
        hi = min(len(data), p + refine_win + 1)
        refined.append(int(lo + np.argmax(envelope[lo:hi])))

    refined.sort()
    deduped = [refined[0]]
    for idx in refined[1:]:
        if idx - deduped[-1] >= min_dist_samples:
            deduped.append(idx)
        elif envelope[idx] > envelope[deduped[-1]]:
            deduped[-1] = idx
    return deduped


# ---------------------------------------------------------------------------
# Per-clap validation
# ---------------------------------------------------------------------------

def validate_clap(data: np.ndarray, sr: int, peak_sample: int, config: dict) -> dict:
    adv = config["advanced"]
    result = {"clipping": "ok", "attack": "ok", "crest": "ok"}
    peak_amp = np.abs(data[peak_sample])

    # 1. Clipping
    win = int(0.005 * sr)
    lo, hi = max(0, peak_sample - win), min(len(data), peak_sample + win + 1)
    if np.max(np.abs(data[lo:hi])) >= adv["clipping_max_linear"]:
        result["clipping"] = "fail"

    # 2. Attack time
    if peak_amp < 1e-10:
        result["attack"] = "fail"
    else:
        thresh = peak_amp * (10 ** (_ATTACK_LEVEL_DB / 20))
        max_samp = int(adv["attack_max_ms"] / 1000 * sr)
        search_start = max(0, peak_sample - max_samp * 3)
        pre = np.abs(data[search_start:peak_sample])
        below = np.where(pre < thresh)[0]
        if len(below) == 0 or (peak_sample - (search_start + below[-1])) > max_samp:
            result["attack"] = "fail"

    # 3. Crest (pre-impulse RMS)
    pre_win = int(0.020 * sr)
    lo_c = max(0, peak_sample - pre_win)
    pre_window = data[lo_c:peak_sample]
    if len(pre_window) < int(0.005 * sr):
        result["crest"] = "fail"
    else:
        rms = np.sqrt(np.mean(pre_window ** 2))
        rms = max(rms, 1e-10)
        if 20 * np.log10(peak_amp / rms) < adv["crest_min_db"]:
            result["crest"] = "fail"

    return result


# ---------------------------------------------------------------------------
# Noise profile measurement
# ---------------------------------------------------------------------------

def measure_noise_profile(profile_path: Path) -> dict:
    """Load a noise profile WAV and return per-band power (linear, not dB).

    Returns {500: power, 1000: power, 2000: power}, or {} on failure.
    Called once per session and passed into analyze_file — keeps I/O out of
    the per-clap hot path.
    """
    try:
        data, sr = load_audio(Path(profile_path))
    except Exception:
        return {}
    if len(data) == 0:
        return {}
    result = {}
    for center in _BAND_CENTERS_HZ:
        sos      = _make_band_filter(center, sr)
        filtered = _apply_band_filter(data, sos)
        result[center] = float(np.mean(filtered ** 2))
    return result


# ---------------------------------------------------------------------------
# Lundeby noise-floor compensation
# ---------------------------------------------------------------------------

def _lundeby_truncation(sch: np.ndarray, sch_db: np.ndarray,
                        filtered: np.ndarray, t: np.ndarray,
                        noise_power_override: float = None) -> tuple:
    """Find where room decay ends and noise floor begins using Lundeby's method.

    Returns (truncation_idx, noise_floor_db_schroeder).

    noise_power_override: if provided (from a pre-recorded noise profile),
    skip tail estimation and use this value directly. More accurate when the
    decay window is too short for the room to go quiet by the end.
    Fallback (when None): estimate noise power from the tail of `filtered`.

    The Schroeder integral's tail is NOT a constant — it declines as
    noise_power × (N - i), so standard Schroeder percentile estimates
    underestimate the noise floor. We build the expected Schroeder noise
    profile from the known noise power and find where the measured Schroeder
    drops within _LUNDEBY_MARGIN_DB of it — that crossing is the truncation
    point where noise starts dominating the measurement.
    """
    n = len(sch_db)
    if n < 20:
        return n, np.nan

    if noise_power_override is not None:
        # Pre-recorded noise profile: fixed estimate, no iteration needed.
        noise_power = noise_power_override
        iterations  = 1
    else:
        # DORMANT BRANCH — iterative tail-based noise estimation.
        #
        # This path fires only when no noise_power_override is provided (i.e., no
        # external noise profile). Test C in v0.4 diagnostics (dev_tools/v0.4_diagnostics.md)
        # showed 0/40 files produced a valid result when this branch was forced on the
        # full parity corpus — every file returns insufficient_decay.
        #
        # Root cause: clap-based impulse responses have insufficient SNR for the algorithm
        # to isolate the noise floor from the decay tail. The decay integrates into ambient
        # noise before the signal goes quiet, so there is no clean tail region to estimate
        # from. This is a fundamental constraint of the measurement method, not a bug.
        #
        # The code is preserved for future recording protocols (sine sweep, balloon pop,
        # extended-duration impulse sources, or the planned web platform phase) that may
        # have the SNR and decay length needed for reliable tail-based noise estimation.
        #
        # Do not remove without confirming the active recording protocol has changed and
        # that the tail-estimation path produces valid results on the new corpus.
        #
        # Initial estimate from the file tail — may be contaminated by remaining
        # decay energy in reverberant rooms, so we iterate below.
        tail_samps  = max(int(n * _LUNDEBY_NOISE_TAIL_FRAC), 200)
        tail_samps  = min(tail_samps, n // 4)
        noise_power = float(np.mean(filtered[-tail_samps:] ** 2))
        iterations  = _LUNDEBY_MAX_ITER

    if noise_power < 1e-30:
        return n, np.nan

    n_total      = len(filtered)
    truncation_idx = n  # start with no truncation

    for _ in range(iterations):
        # Build expected Schroeder noise profile:
        # when only noise remains, sch[i] ≈ noise_power × (N - i).
        remaining    = np.maximum(n_total - np.arange(n), 1)
        noise_sch_db = 10.0 * np.log10(noise_power * remaining / sch[0] + 1e-30)

        # Find where measured Schroeder drops within _LUNDEBY_MARGIN_DB of
        # the noise profile — that crossing is where noise takes over.
        dominated = np.where(sch_db - noise_sch_db < _LUNDEBY_MARGIN_DB)[0]
        if len(dominated) == 0:
            break

        new_trunc = int(np.clip(dominated[0], 10, n - 1))

        if noise_power_override is None:
            # Re-estimate noise from the region past this truncation point.
            # This is cleaner than the file tail because the room has decayed
            # further by this point, so less decay energy contaminates the estimate.
            post_trunc = filtered[new_trunc:]
            if len(post_trunc) >= 50:
                noise_power = float(np.mean(post_trunc ** 2))
                if noise_power < 1e-30:
                    truncation_idx = new_trunc
                    break

        # Converged when truncation index barely moves between iterations.
        if abs(new_trunc - truncation_idx) < 50:
            truncation_idx = new_trunc
            break
        truncation_idx = new_trunc

    if truncation_idx >= n:
        # No crossing found: the decay never came down within _LUNDEBY_MARGIN_DB
        # of the noise profile inside the window, so there is no truncation point
        # to report. Same no-answer contract as the guards above; the NaN floor
        # falls back to the -15 dB fit-start shift downstream. Unguarded, this
        # indexed one past the end of noise_sch_db.
        return n, np.nan
    return truncation_idx, float(noise_sch_db[truncation_idx])


# ---------------------------------------------------------------------------
# Adaptive fit-start detection
# ---------------------------------------------------------------------------

# How many dB of Schroeder curve to use when estimating local linearity at each
# candidate start point.
_FITSTART_SLOPE_WINDOW_DB = 5.0

# Minimum R² of the local slope window to consider the decay linear at that
# point. When a loud clap's direct-sound energy contaminates the early curve,
# the local R² is low (<0.95) until the room decay takes over.
_FITSTART_LOCAL_R2_MIN    = 0.99

# Furthest we'll push the fit start below the nominal upper bound (-5dB).
_FITSTART_MAX_SHIFT_DB    = 15.0


def _find_fit_start(sch_db: np.ndarray, t: np.ndarray,
                    nominal_upper: float, noise_floor_db: float) -> float:
    """Return the dB level at which to start the T20/T30 fit.

    Scans forward from nominal_upper (-5dB) in 1dB steps. At each candidate
    start level, fits a line to the next _FITSTART_SLOPE_WINDOW_DB of the
    Schroeder curve and checks the local R². When R² first exceeds
    _FITSTART_LOCAL_R2_MIN the decay is linear — that is the fit start.

    Using local R² directly avoids the bootstrapping problem of relative-slope
    comparison: a nonlinear early section has low R² regardless of how steep
    it is, and the linear room decay region has high R² regardless of speed.

    Returns nominal_upper (-5dB) unchanged when the curve is already linear
    there (clean moderate-level clap — no adaptation needed).
    """
    search_limit = max(
        nominal_upper - _FITSTART_MAX_SHIFT_DB,
        noise_floor_db + _LUNDEBY_MARGIN_DB + _FITSTART_SLOPE_WINDOW_DB,
    )

    # Guard: if the usable decay is too short relative to the slope window,
    # local R² estimates are unreliable (too few samples per window, noisy).
    # Only attempt adaptation when the curve spans at least 3× the window width.
    decay_span = nominal_upper - max(noise_floor_db + _LUNDEBY_MARGIN_DB, search_limit)
    if decay_span < 3 * _FITSTART_SLOPE_WINDOW_DB:
        return nominal_upper

    # Check whether -5dB is already linear — if so, no shift needed.
    mask0 = (sch_db <= nominal_upper) & (sch_db >= nominal_upper - _FITSTART_SLOPE_WINDOW_DB)
    pts0  = np.where(mask0)[0]
    if len(pts0) >= 5:
        _, _, r0, _, _ = linregress(t[pts0], sch_db[pts0])
        if r0 ** 2 >= _FITSTART_LOCAL_R2_MIN:
            return nominal_upper   # already linear at -5dB, no adaptation

    # Scan deeper for the first linear region.
    for start_db in np.arange(nominal_upper - 1.0, search_limit, -1.0):
        end_db = start_db - _FITSTART_SLOPE_WINDOW_DB
        mask   = (sch_db <= start_db) & (sch_db >= end_db)
        pts    = np.where(mask)[0]
        if len(pts) < 5:
            continue
        _, _, r, _, _ = linregress(t[pts], sch_db[pts])
        if r ** 2 >= _FITSTART_LOCAL_R2_MIN:
            return start_db

    return nominal_upper   # no stable region found — let R² gate handle it


# ---------------------------------------------------------------------------
# Schroeder integration + RT60 per band
# ---------------------------------------------------------------------------

def _fit_window_bounds(fit_window: str) -> tuple:
    if fit_window == "T30":
        return (-5.0, -35.0)
    return (-5.0, -25.0)


def compute_rt60_for_clap(data: np.ndarray, sr: int, peak_sample: int,
                          next_peak_sample, config: dict,
                          noise_powers: dict = None) -> dict:
    """noise_powers: optional {center_hz: linear_power} from measure_noise_profile().
    When provided, Lundeby uses these values instead of estimating from the decay tail."""
    fit_window_str = config["quality"]["fit_window"]
    r_sq_min       = config["quality"]["r_squared_min"]
    upper_db, lower_db = _fit_window_bounds(fit_window_str)

    decay_end = peak_sample + int(_DECAY_WINDOW_MAX_SEC * sr)
    if next_peak_sample is not None:
        decay_end = min(decay_end, next_peak_sample - int(_POST_IMPULSE_BUFFER_SEC * sr))
    decay_end = min(decay_end, len(data))

    decay_slice = data[peak_sample:decay_end]
    filters = {c: _make_band_filter(c, sr) for c in _BAND_CENTERS_HZ}

    rt60_per_band = {}
    fit_quality   = {}

    for center in _BAND_CENTERS_HZ:
        band_key = str(center)
        sos      = filters[center]
        filtered = _apply_band_filter(decay_slice, sos)

        # Schroeder backward integration
        squared = filtered ** 2
        sch     = np.cumsum(squared[::-1])[::-1]

        if sch[0] < 1e-30:
            rt60_per_band[band_key] = None
            fit_quality[band_key]   = {"r_squared": None, "window": fit_window_str,
                                       "reason": "insufficient_energy"}
            continue

        # Normalize Schroeder curve to 0dB at its peak
        sch_peak_idx  = int(np.argmax(sch))
        sch_from_peak = sch[sch_peak_idx:]
        with np.errstate(divide="ignore"):
            sch_db = 10.0 * np.log10(sch_from_peak / sch_from_peak[0] + 1e-30)
        t = np.arange(len(sch_db)) / sr

        # Lundeby: find where noise floor dominates and truncate there.
        # Use the pre-recorded noise profile power for this band if available.
        filtered_from_peak   = filtered[sch_peak_idx:]
        noise_power_for_band = noise_powers.get(center) if noise_powers else None
        trunc_idx, noise_floor_db = _lundeby_truncation(
            sch_from_peak, sch_db, filtered_from_peak, t,
            noise_power_override=noise_power_for_band,
        )
        sch_db_trunc = sch_db[:trunc_idx]
        t_trunc      = t[:trunc_idx]

        # Adaptive fit-start: find where the early Schroeder curve becomes
        # linear. Loud claps in dry rooms have nonlinear direct-sound energy
        # in the first few dB — shifting the start past that region recovers
        # fits that would otherwise fail the R² threshold.
        nf_db      = noise_floor_db if not np.isnan(noise_floor_db) else (upper_db - _FITSTART_MAX_SHIFT_DB)
        fit_upper  = _find_fit_start(sch_db_trunc, t_trunc, upper_db, nf_db)

        # Check the truncated curve reaches the T20/T30 lower bound
        if not np.any(sch_db_trunc <= lower_db):
            # Might still reach the (possibly shifted) fit start — try fitting
            # whatever range is available down to the truncation point.
            if not np.any(sch_db_trunc <= fit_upper):
                rt60_per_band[band_key] = None
                fit_quality[band_key]   = {"r_squared": None, "window": fit_window_str,
                                           "noise_floor_db": round(noise_floor_db, 2) if not np.isnan(noise_floor_db) else None,
                                           "reason": "insufficient_decay"}
                continue
            actual_lower = float(sch_db_trunc[-1])
        else:
            actual_lower = lower_db

        mask = (sch_db_trunc <= fit_upper) & (sch_db_trunc >= actual_lower)
        pts  = np.where(mask)[0]

        if len(pts) < 10:
            rt60_per_band[band_key] = None
            fit_quality[band_key]   = {"r_squared": None, "window": fit_window_str,
                                       "noise_floor_db": round(noise_floor_db, 2) if not np.isnan(noise_floor_db) else None,
                                       "reason": "insufficient_decay"}
            continue

        slope, _, r, _, _ = linregress(t_trunc[pts], sch_db_trunc[pts])
        r_squared = r ** 2

        nf_rounded = round(noise_floor_db, 2) if not np.isnan(noise_floor_db) else None

        if r_squared < r_sq_min:
            rt60_per_band[band_key] = None
            fit_quality[band_key]   = {"r_squared": round(r_squared, 4), "window": fit_window_str,
                                       "noise_floor_db": nf_rounded, "reason": "poor_fit"}
            continue

        if slope >= 0.0:
            rt60_per_band[band_key] = None
            fit_quality[band_key]   = {"r_squared": round(r_squared, 4), "window": fit_window_str,
                                       "noise_floor_db": nf_rounded, "reason": "non_decaying"}
            continue

        rt60 = -60.0 / slope
        if rt60 > _RT60_MAX_PLAUSIBLE_SEC:
            # Extremely shallow slope: noise-floor plateau masquerading as slow decay.
            rt60_per_band[band_key] = None
            fit_quality[band_key]   = {"r_squared": round(r_squared, 4), "window": fit_window_str,
                                       "noise_floor_db": nf_rounded, "reason": "insufficient_decay"}
            continue

        if rt60 < _MIN_RT60_RESOLVABLE_SEC:
            # Room is too dry for T20 to measure reliably. The fit technically
            # passed R² but spans too few samples (~30ms at 0.09s RT60) to be
            # trustworthy. Report as unmeasured rather than a wrong number.
            rt60_per_band[band_key] = None
            fit_quality[band_key]   = {"r_squared": round(r_squared, 4), "window": fit_window_str,
                                       "noise_floor_db": nf_rounded, "reason": "below_measurement_floor"}
            continue

        rt60_per_band[band_key] = round(rt60, 4)
        fit_quality[band_key]   = {"r_squared": round(r_squared, 4), "window": fit_window_str,
                                   "noise_floor_db": nf_rounded,
                                   "fit_start_db": round(fit_upper, 1),
                                   "reason": "ok"}

    return {"rt60_per_band": rt60_per_band, "fit_quality": fit_quality}


# ---------------------------------------------------------------------------
# Summary computation
# ---------------------------------------------------------------------------

def _median_or_none(values: list):
    valid = [v for v in values if v is not None and not np.isnan(v)]
    if not valid:
        return None
    return round(float(np.median(valid)), 4)


def _variance_or_none(values: list):
    valid = [v for v in values if v is not None and not np.isnan(v)]
    if len(valid) < 2:
        return None
    return round(float(np.var(valid, ddof=1)), 4)


def _count_outliers(values: list, threshold_factor: float = 1.5) -> int:
    valid = [v for v in values if v is not None and not np.isnan(v)]
    if len(valid) < 4:
        return 0
    arr = np.array(valid)
    q1, q3 = np.percentile(arr, [25, 75])
    iqr = q3 - q1
    lo, hi = q1 - threshold_factor * iqr, q3 + threshold_factor * iqr
    return int(np.sum((arr < lo) | (arr > hi)))


def build_summary(clap_results: list, config: dict) -> dict:
    valid_claps = [c for c in clap_results if c["valid"]]
    summary = {
        "n_claps_total":              len(clap_results),
        "n_claps_valid":              len(valid_claps),
        "rt60_median_per_band":       {"500": None, "1000": None, "2000": None},
        "rt60_gate_metric":           None,
        "rt60_gate_metric_definition": "median of 1kHz octave band RT60 across valid claps",
        "variance_per_band":          {"500": None, "1000": None, "2000": None},
        "low_band_confidence":        "low",
        "outlier_count":              0,
    }
    if not valid_claps:
        return summary
    for band_key in ["500", "1000", "2000"]:
        vals = [c["rt60_per_band"].get(band_key) for c in valid_claps]
        summary["rt60_median_per_band"][band_key] = _median_or_none(vals)
        summary["variance_per_band"][band_key]    = _variance_or_none(vals)
    summary["rt60_gate_metric"] = summary["rt60_median_per_band"]["1000"]
    vals_1k = [c["rt60_per_band"].get("1000") for c in valid_claps]
    summary["outlier_count"] = _count_outliers(vals_1k)
    return summary


# ---------------------------------------------------------------------------
# Disposition
# ---------------------------------------------------------------------------

# Per-clap fit reasons that mean the fit itself was untrustworthy, as opposed to
# trustworthy-but-too-short. ANY of these on a valid clap disqualifies the take from
# the dry-room override: they are the scatter a processing artifact produces, and the
# whole point of the rule is to distinguish that from a genuinely dry room.
_DRY_ROOM_DISQUALIFYING_REASONS = ("poor_fit", "non_decaying", "insufficient_decay")


def _dry_room_tally(clap_results: list, config: dict) -> dict | None:
    """AB-7' (Rick, 2026-07-30): WITHIN-TAKE clap agreement on a room too dry to measure.

    Returns None when the profile does not opt in (`gate.dry_room_pass` absent or
    false) — so this is inert for DEFAULT_CONFIG, strict, forgiving, the standalone
    tools and the frozen screening demo, none of which set the key. Otherwise returns
    the tally, with `agrees` saying whether the take qualifies.

    THE PROBLEM IT SOLVES: `_MIN_RT60_RESOLVABLE_SEC` (0.10 s) discards a fit that
    PASSED R² merely for spanning too little time, per band per clap. When that happens
    on every valid clap, `rt60_gate_metric` is None and the take reads
    `error: rt60_1khz_unavailable`, which blocks a live session — so the gate refuses
    the BEST rooms, a treated space being exactly the case at risk.

    THE TEST, and why it is within-take rather than take-to-take: a genuinely dry room
    produces the same verdict on clap after clap with fits that pass the quality bar,
    while a processing artifact scatters across `poor_fit` / `non_decaying` /
    `insufficient_decay`. One take already carries 9-11 independent claps, so the
    stability sample exists inside a single recording — and the gate is a LIVE BLOCKING
    gate, so a take-to-take rule could not decide take 1, which is the take being
    blocked.

    Deliberately NOT touching `_MIN_RT60_RESOLVABLE_SEC`: it is triplicated across v4,
    v5 and the Test Package copy, and moving it would move every consumer including the
    standalone tools. This reads the floor's OUTCOME instead of relaxing the floor.
    """
    if not config["gate"].get("dry_room_pass"):
        return None

    min_valid   = config["gate"]["min_valid_claps"]
    r_sq_min    = config["quality"]["r_squared_min"]
    valid_claps = [c for c in clap_results if c["valid"]]

    below = disqualifying = 0
    r_squareds = []
    for c in valid_claps:
        fq     = (c.get("fit_quality") or {}).get("1000") or {}
        reason = fq.get("reason")
        r_sq   = fq.get("r_squared")
        if reason in _DRY_ROOM_DISQUALIFYING_REASONS:
            disqualifying += 1
        elif reason == "below_measurement_floor" and r_sq is not None and r_sq >= r_sq_min:
            below += 1
            r_squareds.append(r_sq)

    # `clap_invalid` never appears here (those claps are filtered by c["valid"]), and an
    # `ok` clap is not counted either way — an `ok` clap means a metric EXISTS, so the
    # take would not have reached `rt60_1khz_unavailable` at all.
    agrees = below >= min_valid and disqualifying == 0
    tally = {
        "enabled":            True,
        "agrees":             agrees,
        "n_claps_valid":      len(valid_claps),
        "n_below_floor":      below,
        "n_disqualifying":    disqualifying,
        "min_valid_claps":    min_valid,
        "r_squared_min":      r_sq_min,
        "r_squared_min_seen": round(min(r_squareds), 4) if r_squareds else None,
        "reason": (
            f"rt60_1khz_below_measurement_floor_treated "
            f"({below} of {len(valid_claps)} valid claps agree, "
            f"r2>={min(r_squareds):.4f}, none disqualifying)"
            if agrees else None
        ),
    }
    return tally


def resolve_disposition(clap_results: list, summary: dict, config: dict) -> tuple:
    """The take's FINAL disposition: `compute_disposition` plus the AB-7' dry-room
    override. Returns (disposition, reason, dry_room_tally).

    ONE PLACE, deliberately. The override cannot live inside `compute_disposition`
    because its evidence is PER-CLAP and that function only ever sees the summary —
    and it must not live at the three platform call sites, because "one policy, many
    call sites" is exactly the split that produced the two AB-5 plumbing defects.
    Every consumer that wants the ruled behaviour calls THIS.

    `compute_disposition` keeps its old signature and old behaviour untouched, so the
    backfill (which re-derives from a stored scalar and has no clap detail) and any
    other summary-only caller are unaffected.
    """
    disposition, reason = compute_disposition(summary, config)
    dry_room = _dry_room_tally(clap_results, config)
    if (dry_room is not None and dry_room["agrees"]
            and disposition == "error" and reason == "rt60_1khz_unavailable"):
        # Narrow by construction: ONLY the could-not-measure token is overridden.
        # A `reject` (too live), an `insufficient_valid_claps` (bad claps) and a
        # `no_valid_claps` are all left exactly as they were — a widened band must
        # never rescue a take whose MEASUREMENT never happened, which is the same
        # invariant the backfill's validity guard enforces on the stored half.
        return "pass", dry_room["reason"], dry_room

    # min_usable_claps (Rick, 2026-08-07): `min_valid_claps` gates clap QUALITY
    # (peak, clipping, attack, crest); nothing gated how many claps actually
    # SUPPORTED the number — the gate metric is a median over claps with a
    # non-None 1 kHz value, so a take with 9 valid claps and 1 usable value was
    # tiered on that single clap. Ruled on live evidence: the one observed
    # wrong-tier verdict rested on 2 disagreeing claps (0.6016 borderline-FAIL)
    # and its re-run read 6/6 usable at 0.4912, production-PASS. Below the floor
    # the take returns error — an honest retry, not a room verdict.
    # Optional key, absent = byte-identical (the AB-2 / AB-7' pattern): strict,
    # forgiving, the standalone tools and the frozen demo do not set it.
    # Mutually exclusive with the dry-room override above by construction: that
    # fires only when NO metric exists, this only when one does.
    # Fires only on takes that CLEARED the validity gate: a take failing
    # min_valid_claps keeps insufficient_valid_claps — first failure in the
    # pipeline wins, and the tester fix advice differs between the two.
    min_usable = config["gate"].get("min_usable_claps")
    if (min_usable is not None
            and summary["rt60_gate_metric"] is not None
            and summary["n_claps_valid"] >= config["gate"]["min_valid_claps"]):
        n_usable = sum(1 for c in clap_results
                       if c["valid"] and c["rt60_per_band"].get("1000") is not None)
        if n_usable < min_usable:
            return ("error",
                    f"insufficient_usable_claps ({n_usable} of {min_usable} required)",
                    dry_room)
    return disposition, reason, dry_room


def compute_disposition(summary: dict, config: dict) -> tuple:
    n_total     = summary["n_claps_total"]
    n_valid     = summary["n_claps_valid"]
    min_valid   = config["gate"]["min_valid_claps"]
    gate_metric = summary["rt60_gate_metric"]

    if n_total == 0 or n_valid == 0:
        return "error", "no_valid_claps"
    if n_valid < min_valid:
        return "reject", f"insufficient_valid_claps ({n_valid} of {min_valid} required)"
    if gate_metric is None:
        return "error", "rt60_1khz_unavailable"

    reject_above = config["gate"]["reject_above"]
    pass_below   = config["gate"]["pass_below"]
    # Optional FOURTH band (audio band policy AB-2, Rick 2026-07-30). When set, the
    # span above pass_below splits: <= production_below is "production" (a usable
    # room needing more edit effort), above it is "borderline". ABSENT (the default,
    # and both shipped profiles) => production_below == reject_above => the
    # production branch can never fire and this is byte-identical to the three-band
    # behaviour every existing consumer depends on. That inertness is the point: the
    # standalone tools and the strict/forgiving profiles must not move.
    production_below = config["gate"].get("production_below")

    if gate_metric > reject_above:
        return "reject",     f"rt60_1khz_{gate_metric:.3f}s_exceeds_limit_{reject_above}s"
    if gate_metric <= pass_below:
        return "pass",       f"rt60_1khz_{gate_metric:.3f}s_below_threshold_{pass_below}s"
    if production_below is not None and gate_metric <= production_below:
        return "production", f"rt60_1khz_{gate_metric:.3f}s_between_{pass_below}s_and_{production_below}s"
    lower = production_below if production_below is not None else pass_below
    return     "borderline", f"rt60_1khz_{gate_metric:.3f}s_between_{lower}s_and_{reject_above}s"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def analyze_file(file_path: Path, config: dict = None,
                 noise_profile_path: Path = None,
                 talent_id: str = None, language_code: str = None) -> dict:
    """Analyze a clap recording and return RT60 measurements.

    noise_profile_path: optional path to a WAV of ambient room noise recorded
    before the claps. When provided, per-band noise power is measured from it
    and passed to Lundeby instead of being estimated from the decay tail.
    Falls back to tail estimation automatically when omitted.

    talent_id / language_code: optional identity (cross-pipeline convention,
    level_check_output_contract.md §2.1). Stamped into every result (including
    error results) so an RT60 record is self-identifying. Unsupplied stays "" so
    the field type is always str. Appended after noise_profile_path to keep the
    existing positional callers (analyze_file(wav, config, noise_profile_path=...))
    working unchanged.
    """
    if config is None:
        config = _deep_copy_dict(DEFAULT_CONFIG)

    file_path  = Path(file_path)
    scanned_at = datetime.now(timezone.utc).isoformat()

    # Load noise profile once — None if not provided or unreadable.
    noise_powers = None
    if noise_profile_path is not None:
        noise_powers = measure_noise_profile(Path(noise_profile_path))
        if not noise_powers:
            noise_powers = None   # treat unreadable profile as absent

    def _error_result(reason: str) -> dict:
        return {
            "file": file_path.name, "sample_rate": None, "duration_sec": None,
            "scanned_at": scanned_at, "scanner_version": SCANNER_VERSION,
            "config_profile": config.get("profile_name", "custom"),
            "claps": [], "summary": build_summary([], config),
            "disposition": "error", "disposition_reason": reason,
            "talent_id": talent_id or "", "language_code": language_code or "",
        }

    try:
        data, sr = load_audio(file_path)
    except Exception as e:
        return _error_result(f"load_failed: {e}")

    if len(data) == 0:
        return _error_result("empty_file")

    duration_sec  = len(data) / sr
    clap_samples  = detect_claps(data, sr, config)
    clap_results  = []

    for i, peak_sample in enumerate(clap_samples):
        next_sample = clap_samples[i + 1] if i + 1 < len(clap_samples) else None
        validation  = validate_clap(data, sr, peak_sample, config)
        is_valid    = all(v == "ok" for v in validation.values())

        if is_valid:
            band_result = compute_rt60_for_clap(
                data, sr, peak_sample, next_sample, config, noise_powers=noise_powers
            )
        else:
            fw = config["quality"]["fit_window"]
            null_bands = {str(c): None for c in _BAND_CENTERS_HZ}
            null_fits  = {str(c): {"r_squared": None, "window": fw, "reason": "clap_invalid"}
                          for c in _BAND_CENTERS_HZ}
            band_result = {"rt60_per_band": null_bands, "fit_quality": null_fits}

        peak_amp  = float(np.abs(data[peak_sample]))
        peak_dbfs = round(20 * np.log10(peak_amp + 1e-10), 2)

        clap_results.append({
            "index":         i,
            "timestamp_sec": round(peak_sample / sr, 6),
            "peak_dbfs":     peak_dbfs,
            "valid":         is_valid,
            "validation":    validation,
            "rt60_per_band": band_result["rt60_per_band"],
            "fit_quality":   band_result["fit_quality"],
        })

    summary     = build_summary(clap_results, config)
    disposition, disposition_reason, dry_room = resolve_disposition(
        clap_results, summary, config)

    return {
        "file":                file_path.name,
        "sample_rate":         sr,
        "duration_sec":        round(duration_sec, 4),
        "scanned_at":          scanned_at,
        "scanner_version":     SCANNER_VERSION,
        "config_profile":      config.get("profile_name", "custom"),
        "noise_profile_used":  Path(noise_profile_path).name if noise_powers else None,
        "claps":               clap_results,
        "summary":             summary,
        "disposition":         disposition,
        "disposition_reason":  disposition_reason,
        # AB-7': the per-clap agreement tally, kept whenever the rule was EVALUATED
        # (None when the profile does not enable it). Present even when the rule
        # DECLINED, because "we looked and the claps disagreed" is the auditable
        # half — and the caller cannot recover it later: per-clap detail is dropped
        # before storage (platform _set_rec_qc keeps summary fields only).
        "dry_room":            dry_room,
        "talent_id":           talent_id or "",
        "language_code":       language_code or "",
    }
