# screening_pipeline_core.py — v1
#
# Orchestration layer for the three-stage talent-environment screening suite.
# Sequences, in order, gating on each stage's verdict:
#
#   1. level check   (level_check_phrase_core.scan_phrase)
#   2. noise profile (noise_profile_scanner_core.scan_noise_profile)
#   3. RT60          (rt60_analyzer_core_v5.analyze_file)
#
# Stop-and-report on the first non-PASS stage; thread forward on PASS. The
# pipeline is the point where "one applicant, three scans" becomes real, so it
# also (a) threads talent_id + language_code across all three stage records and
# (b) hands off the two cross-stage values the cores were designed to share:
#   - speech_level_dba: measured once by the level check, passed straight to the
#     noise scan's SNR step (locked §3 handoff — no re-measure).
#   - the noise scan's trimmed clean-noise WAV: fed to RT60 as its Lundeby noise
#     profile (RT60's tail estimation is non-functional without one).
#
# Pure logic, no __main__ — importable by a future API/GUI layer, matching the
# "core is pure logic" pattern of the three cores it drives. Identity is passed
# NATIVELY into each core (talent_id + language_code kwargs) — the cores persist it
# in their own sidecars/JSONL, so every per-tool record is self-identifying. This
# replaces the earlier post-hoc injection (see level_check_output_contract.md §2.1:
# the native core retrofit landed, so the consolidated record and the per-tool
# records now agree at the source, with no re-stamping step).

import sys
import json
import math
import importlib.util
import numpy as np
from pathlib import Path
from datetime import datetime, timezone


# ------------------------------------------------------------
# Identity constants (stamped into every consolidated record)
# ------------------------------------------------------------
PIPELINE_VERSION = "screening_pipeline_core_v1"
SCHEMA_VERSION = "1.0"

# Consolidated audit log: one JSON line per pipeline run. Shares the suite's
# Application Support/MAD/ location with the per-tool logs (one file per tool).
DEFAULT_LOG_PATH = Path.home() / "Library" / "Application Support" / "MAD" / "screening_pipeline_runs.jsonl"

STAGE_ORDER = ("level_check", "noise_profile", "rt60")

# ------------------------------------------------------------
# Core module locations. Default to the canonical MAD sibling-folder layout,
# resolved relative to THIS file so it travels with the MAD tree (folder names
# contain spaces, so the cores are loaded by absolute path, not by `import`).
# Override via run_screening_pipeline(core_paths={...}) for tests / relocations.
# ------------------------------------------------------------
_MAD_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CORE_PATHS = {
    "level_check": _MAD_ROOT / "Level Check Phrase Test" / "level_check_phrase_core.py",
    "noise_profile": _MAD_ROOT / "Noise Profile Scanner" / "noise_profile_scanner_core.py",
    "rt60": _MAD_ROOT / "RT60 Analyzer" / "rt60_analyzer_core_v5.py",
}
# Distinct sys.modules names so the cores' identically-named symbols
# (load_audio, _json_safe, DEFAULT_CONFIG, SCANNER_VERSION, ...) never collide.
_CORE_MODULE_NAMES = {
    "level_check": "mad_level_check_phrase_core",
    "noise_profile": "mad_noise_profile_scanner_core",
    "rt60": "mad_rt60_analyzer_core_v5",
}

# Input-file discovery (used only when an explicit path is not supplied).
# Phrase/noise/clap suffixes are all the suite's locked conventions, so discovery
# is unambiguous by suffix alone.
_PHRASE_SUFFIXES = ("-level", "_level", "-phrase", "_phrase")
_NOISE_SUFFIXES = ("-blank", "_blank", "-noise", "_noise")  # raw dead-air input family
# The noise scanner's trimmed OUTPUT now carries a distinct `_np` suffix (outside
# _NOISE_SUFFIXES), so discover_noise can never pick it up as raw input and needs no
# exclusion — the old raw-vs-`_noise`-output collision is gone.
_CLAP_SUFFIXES = ("-clap", "_clap")   # locked canonical clap-recording suffix

# Cache of loaded core modules, keyed by resolved absolute path.
_CORE_CACHE = {}


# ------------------------------------------------------------
# Core loading
# ------------------------------------------------------------
def _load_one_core(module_name: str, path: Path):
    """Load a single core module from an absolute file path (folder names have
    spaces, so spec_from_file_location is used rather than a normal import).
    Cached by path so repeated runs don't re-exec the module."""
    path = Path(path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Core module not found: {path}")
    key = str(path)
    if key in _CORE_CACHE:
        return _CORE_CACHE[key]
    spec = importlib.util.spec_from_file_location(module_name, key)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot build import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module   # so the module's own __file__-relative logic resolves
    spec.loader.exec_module(module)
    _CORE_CACHE[key] = module
    return module


class _Cores:
    """Thin handle bundling the three loaded core modules."""
    def __init__(self, level_check, noise_profile, rt60):
        self.level_check = level_check
        self.noise_profile = noise_profile
        self.rt60 = rt60


def load_cores(*, level_check=None, noise_profile=None, rt60=None) -> _Cores:
    """Load (and cache) the three core modules. Each argument optionally
    overrides the default path for that core."""
    paths = {
        "level_check": level_check or _DEFAULT_CORE_PATHS["level_check"],
        "noise_profile": noise_profile or _DEFAULT_CORE_PATHS["noise_profile"],
        "rt60": rt60 or _DEFAULT_CORE_PATHS["rt60"],
    }
    return _Cores(
        level_check=_load_one_core(_CORE_MODULE_NAMES["level_check"], paths["level_check"]),
        noise_profile=_load_one_core(_CORE_MODULE_NAMES["noise_profile"], paths["noise_profile"]),
        rt60=_load_one_core(_CORE_MODULE_NAMES["rt60"], paths["rt60"]),
    )


# ------------------------------------------------------------
# Input discovery (explicit override always wins; this is the fallback)
# ------------------------------------------------------------
def discover_phrase(folder: Path) -> Path | None:
    """First *.wav whose stem ends with a level-check phrase suffix."""
    for f in sorted(Path(folder).glob("*.wav")):
        if any(f.stem.lower().endswith(s) for s in _PHRASE_SUFFIXES):
            return f
    return None


def discover_noise(folder: Path) -> Path | None:
    """First *.wav whose stem ends with a raw dead-air suffix
    (-blank/_blank/-noise/_noise). The scanner's trimmed output now carries a
    distinct `_np` suffix outside this family, so it is never a candidate here and
    no exclusion is needed (the prior raw-vs-`_noise`-output collision is gone)."""
    for f in sorted(Path(folder).glob("*.wav")):
        if any(f.stem.lower().endswith(s) for s in _NOISE_SUFFIXES):
            return f
    return None


def discover_clap(folder: Path, exclude=()) -> Path | None:
    """First *.wav whose stem ends with the locked clap suffix (`_clap`/`-clap`).
    Files already matched as phrase/noise are excluded. Returns None if nothing
    matches — pass clap_wav explicitly to override."""
    ex = {Path(p).resolve() for p in exclude if p}
    wavs = [f for f in sorted(Path(folder).glob("*.wav")) if f.resolve() not in ex]
    for f in wavs:
        if any(f.stem.lower().endswith(s) for s in _CLAP_SUFFIXES):
            return f
    return None


# ------------------------------------------------------------
# Gate-verdict normalization
#
# Each core speaks a different verdict vocabulary; the pipeline normalizes every
# stage to one of: "PASS" (thread forward / route the applicant on),
# "FAIL" (a content gate failed — talent/room below bar), or "ERROR" (the stage
# could not be evaluated — missing input, unreadable file, no measurable result;
# route to re-record/fix, NOT to offboarding). The stage's NATIVE bucket
# (peak/lufs dispositions, noise WARN/CAUTION/PASS, RT60 pass/borderline) is kept
# in the record alongside the gate verdict so downstream pay-rate / offboarding /
# QA-calibration analytics keep the granular signal, not just pass/fail.
# ------------------------------------------------------------
def _gate_level_check(rec: dict) -> tuple[str, str | None]:
    if rec.get("verdict") == "PASS":
        return "PASS", None
    return "FAIL", "; ".join(rec.get("fail_reasons") or []) or "level check failed"


def _gate_noise(rec: dict) -> tuple[str, str | None]:
    if rec.get("verdict") == "PASS":
        return "PASS", None
    return "FAIL", rec.get("gate_reason") or "noise gate failed"


def _gate_rt60(rec: dict, borderline_pass: bool) -> tuple[str, str | None]:
    disp = rec.get("disposition")
    reason = rec.get("disposition_reason")
    if disp == "pass":
        return "PASS", reason
    if disp == "production":
        # Fourth band (audio band policy AB-2, Rick 2026-07-30) — emitted ONLY by a
        # profile carrying gate.production_below. A usable room that needs more edit
        # effort: it PASSES the gate and carries its cost as a TIER CEILING, not as a
        # refusal. Unconditional, unlike borderline: `production` sits by construction
        # inside a band the profile author declared acceptable, so there is no
        # caller-side policy left to apply. Additive and unreachable for the two
        # shipped profiles, neither of which sets that key — WITHOUT this branch an
        # unknown token would fall to ERROR below, which is why it is not optional.
        return "PASS", reason
    if disp == "borderline":
        return ("PASS" if borderline_pass else "FAIL"), reason
    if disp == "reject":
        return "FAIL", reason
    # "error" or anything unexpected: the room could not be evaluated.
    return "ERROR", reason


# ------------------------------------------------------------
# JSON safety
# ------------------------------------------------------------
def _json_safe(value):
    """Recursively coerce into strict-JSON-safe form (mirrors the cores'
    _json_safe): NaN/Inf -> None, Path -> str, numpy scalar -> python scalar,
    dict/list recursed. The stage dicts are already safe from their own cores;
    this guards the consolidated wrapper and RT60's record (which has no safety
    pass of its own)."""
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    return value


def _s(p) -> str | None:
    return str(p) if p else None


# ------------------------------------------------------------
# Artifact writers
# ------------------------------------------------------------
def append_pipeline_log(record: dict, log_path: Path = None) -> Path:
    """Append one compact, independently-parseable JSON line to the consolidated
    pipeline log (same convention as the per-tool logs)."""
    log_path = Path(log_path) if log_path else DEFAULT_LOG_PATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(",", ":"))
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return log_path


# ------------------------------------------------------------
# Consolidated record assembly — THE single source-of-truth dict.
# ------------------------------------------------------------
def _build_consolidated(
    *,
    session_folder: Path,
    talent_id: str,
    language_code: str,
    inputs: dict,
    pipeline_verdict: str,
    gated_at_stage: str | None,
    stage_results: list,
    stages: dict,
    errors: list,
) -> dict:
    record = {
        "schema_version": SCHEMA_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "talent_id": talent_id,
        "language_code": language_code,
        "session_folder": str(session_folder),
        "inputs": inputs,
        "pipeline_verdict": pipeline_verdict,   # PASS | FAIL | ERROR
        "gated_at_stage": gated_at_stage,        # None when every stage PASSed
        "stage_order": list(STAGE_ORDER),
        "stage_results": stage_results,          # ordered gate summary, only stages run
        "stages": stages,                        # full identity-injected stage dicts (None if not reached)
        "errors": errors,                        # pipeline-level (non-gate) problems
    }
    return _json_safe(record)


# ------------------------------------------------------------
# Top-level orchestration
# ------------------------------------------------------------
def run_screening_pipeline(
    session_folder,
    *,
    talent_id: str = "",
    language_code: str = "",
    phrase_wav=None,
    noise_wav=None,
    clap_wav=None,
    level_check_config: dict = None,
    noise_config: dict = None,
    rt60_config: dict = None,
    rt60_borderline_pass: bool = True,
    write_stage_outputs: bool = True,
    write_pipeline_log: bool = True,
    pipeline_log_path=None,
    core_paths: dict = None,
) -> dict:
    """Run the three-stage screening gate on a session folder and return the
    consolidated record.

    Sequence: level check -> noise profile -> RT60. Gating stops at the first
    stage whose normalized verdict is not PASS (FAIL = content gate; ERROR =
    could-not-evaluate); later stages are not run. Identity (talent_id +
    language_code) is passed NATIVELY into each core, which persists it in its own
    record; the same values go on the consolidated record. No post-hoc stamping.

    rt60_borderline_pass defaults True (locked policy): a borderline RT60 routes
    to PASS and advances the applicant. Kept as a knob for callers who want the
    stricter behavior, but the default is the decided suite policy.

    Cross-stage handoffs wired here:
      - speech_level_dba: read from the level-check result and passed straight to
        scan_noise_profile(speech_level_dba=...) — measured once, never re-read
        (locked §3 contract).
      - noise scan output WAV (trimmed clean noise): passed to RT60 as its
        noise_profile_path for Lundeby compensation. Falls back to the raw noise
        input when no trimmed output exists (e.g. write_stage_outputs=False).

    Inputs are resolved up front (explicit *_wav overrides win; otherwise folder
    discovery) BEFORE any stage runs. The noise scan's trimmed `_np` output is
    outside the raw-noise discovery family anyway, so it can never pollute
    discovery. A missing/unreadable input for a stage that is reached yields an
    ERROR at that stage rather than an exception.

    write_stage_outputs: when True the cores write their own per-tool artifacts
    (and the trimmed *_np.wav RT60 needs). When False, nothing per-tool is
    written and RT60 falls back to the raw noise input as its profile.

    Returns the single source-of-truth consolidated dict (strict-JSON-safe).
    """
    session_folder = Path(session_folder)
    if not session_folder.is_dir():
        raise NotADirectoryError(f"session_folder is not a directory: {session_folder}")

    cores = load_cores(**(core_paths or {}))

    # Resolve all inputs up front (before any stage writes output).
    phrase = Path(phrase_wav) if phrase_wav else discover_phrase(session_folder)
    noise = Path(noise_wav) if noise_wav else discover_noise(session_folder)
    clap = Path(clap_wav) if clap_wav else discover_clap(session_folder, exclude=(phrase, noise))
    inputs = {"phrase_wav": _s(phrase), "noise_wav": _s(noise), "clap_wav": _s(clap)}

    stages = {"level_check": None, "noise_profile": None, "rt60": None}
    stage_results = []
    errors = []

    def finalize(pipeline_verdict, gated_at_stage):
        record = _build_consolidated(
            session_folder=session_folder,
            talent_id=talent_id,
            language_code=language_code,
            inputs=inputs,
            pipeline_verdict=pipeline_verdict,
            gated_at_stage=gated_at_stage,
            stage_results=stage_results,
            stages=stages,
            errors=errors,
        )
        if write_pipeline_log:
            append_pipeline_log(record, pipeline_log_path)
        return record

    def stage_error(stage, reason, disposition=None):
        """Record an ERROR for a stage and finalize (gating stops here)."""
        errors.append(f"{stage}: {reason}")
        stage_results.append({"stage": stage, "verdict": "ERROR",
                              "disposition": disposition, "reason": reason})
        return finalize("ERROR", stage)

    # ---- Stage 1: level check ----
    if phrase is None or not Path(phrase).exists():
        return stage_error("level_check", "no level-check phrase WAV found "
                           "(expected a *_phrase/*_level WAV, or pass phrase_wav=...)")
    try:
        lc = cores.level_check.scan_phrase(
            phrase, config=level_check_config, write_outputs=write_stage_outputs,
            talent_id=talent_id, language_code=language_code)
    except Exception as e:
        return stage_error("level_check", f"scan_phrase raised: {e}")
    stages["level_check"] = lc

    verdict, reason = _gate_level_check(lc)
    stage_results.append({
        "stage": "level_check", "verdict": verdict,
        "disposition": {"peak": lc.get("peak_disposition"), "lufs": lc.get("lufs_disposition")},
        "reason": reason,
    })
    if verdict != "PASS":
        return finalize(verdict, "level_check")

    # Locked §3 handoff: the level check measured speech_level_dba once. A PASS
    # implies it is measurable; guard the impossible-but-fatal case so the noise
    # scan never raises its "no speech level" setup error mid-pipeline.
    speech_level_dba = lc.get("speech_level_dba")
    if speech_level_dba is None:
        stage_results[-1]["verdict"] = "ERROR"
        stage_results[-1]["reason"] = "speech_level_dba unmeasurable; cannot run noise SNR"
        errors.append("level_check: passed but speech_level_dba is None")
        return finalize("ERROR", "level_check")

    # ---- Stage 2: noise profile ----
    if noise is None or not Path(noise).exists():
        return stage_error("noise_profile", "no noise WAV found "
                           "(expected a *-blank/*-noise WAV, or pass noise_wav=...)")
    try:
        ns = cores.noise_profile.scan_noise_profile(
            noise, modality="session", speech_level_dba=speech_level_dba,
            config=noise_config, write_outputs=write_stage_outputs,
            talent_id=talent_id, language_code=language_code)
    except Exception as e:
        return stage_error("noise_profile", f"scan_noise_profile raised: {e}")
    stages["noise_profile"] = ns

    verdict, reason = _gate_noise(ns)
    stage_results.append({
        "stage": "noise_profile", "verdict": verdict,
        "disposition": ns.get("disposition"), "reason": reason,
    })
    if verdict != "PASS":
        return finalize(verdict, "noise_profile")

    # ---- Stage 3: RT60 ----
    if clap is None or not Path(clap).exists():
        return stage_error("rt60", "no clap WAV found "
                           "(expected a *_clap/*-clap WAV, or pass clap_wav=...)")
    # Prefer the trimmed certified-clean noise from stage 2; fall back to the raw
    # noise input (e.g. dry-run, when no *_np.wav was written).
    rt60_noise_profile = ns.get("output_wav") or str(noise)
    try:
        rt = cores.rt60.analyze_file(
            clap, config=rt60_config, noise_profile_path=rt60_noise_profile,
            talent_id=talent_id, language_code=language_code)
    except Exception as e:
        return stage_error("rt60", f"analyze_file raised: {e}")
    stages["rt60"] = rt

    verdict, reason = _gate_rt60(rt, rt60_borderline_pass)
    stage_results.append({
        "stage": "rt60", "verdict": verdict,
        "disposition": rt.get("disposition"), "reason": reason,
    })
    if verdict != "PASS":
        return finalize(verdict, "rt60")

    # All three stages PASSed.
    return finalize("PASS", None)
