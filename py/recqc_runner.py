"""
recqc_runner.py - RecQC's thin orchestration over the vendored platform cores.

RecQC code, not vendored. It calls the cores the way the live platform does
(MAD/Talent Platform app/screening.py and app/cores.py), so a take scores here as it
would on the platform:

    working the cores read a 48 kHz, 24-bit, mono working copy, made as the platform's
            ingest_capture makes it: a take already in that format is its own copy,
            anything else goes through the platform's _derive_working (soxr)
    level   scan_phrase(wav, write_outputs=False); a PASS with no measurable speech
            level becomes ERROR, as on the platform
    noise   scan_noise_profile(wav, modality="session", speech_level_dba=<from level>,
            config=<profile>, write_outputs=True, log_path=<per take>); on PASS the
            core writes <take>_np.wav, the trimmed room tone RT60 uses
    rt60    analyze_file(wav, config=<profile>, noise_profile_path=<the _np.wav>)
    gates   screening_pipeline_core._gate_level_check, _gate_noise and
            _gate_rt60(borderline_pass=True), the functions the platform calls
    format  provenance.verify_provenance on each take as captured, before the working
            copy (ingest_capture's order)
    profile one per core, as the platform has one setting per core (noise_profile_name,
            rt60_profile_name): a shipped yaml, the core's built-in config, or a yaml
            the user picked; results carry its name and config_sha256 (the AB-6 stamp)
    clap    rt60_in_band, the platform's rule for whether room reflection runs at all
            (not in the lo-fi band, noise WARN); the takes joined by concat_clap_takes
            when more claps were asked for; clap_collection_state, whether to ask
    ceiling the platform's tier matrix: min(format, environment, device) from
            _env_ceiling and _device_ceiling, named by AUDIO_TIERS

The entry points. run_folder scores one folder of takes (a phrase, a noise and a clap
WAV) with no browser and no mic; every stage that can run does run, for coverage, and
the parity test diffs its records between CPython and Pyodide. The page's takes go
through run_level_take (M2), run_noise_take (M3) and run_clap_take (M4), one call per
take, in the platform's stage order: the quiet room runs only after the level gate
passed, room reflection only after the quiet-room gate passed and only outside the
lo-fi band. summary turns the page's results into the ceiling card and the per-metric
margins. run_page_flow runs that order over a folder of takes, so the parity test
covers the page's path too. The results are plain JSON-safe dicts, so the same
functions run in CPython and in Pyodide.

Usage:  python3 py/recqc_runner.py <folder of takes, or a folder of such folders> [profile]
"""
import json
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import yaml

# Importing the cores package runs the start check before any vendored module loads.
from cores import CORES_DIR
from cores import level_check_phrase_core as level_core
from cores import noise_profile_scanner_core as noise_core
from cores import platform_ceiling
from cores import platform_clap
from cores import platform_ingest
from cores import platform_stages
from cores import provenance
from cores import rt60_analyzer_core_v5 as rt60_core
from cores import screening_pipeline_core as spc
from cores import tiers

# M1 profiles. "default" is each core's built-in config (what the stored corpus
# records used); "platform_band" is the policy the live platform runs.
PROFILES = ("default", "platform_band")

# The page's selector: shipped profiles per core (Rick 2026-10-22, one selector per
# core). "default" is the core's built-in config, what the platform runs when its
# setting is empty. The RT60 core's built-in config is Strict (same name and numbers as
# configs/rt60/strict.yaml), so RT60 lists Strict alone. There is no noise Strict or
# Forgiving, here or on the platform.
SHIPPED_PROFILES = {"noise": ("platform_band", "default"),
                    "rt60": ("platform_band", "strict", "forgiving")}
CORE_MODULES = {"noise": noise_core, "rt60": rt60_core}
# The section a user's yaml must carry for each core, and the core's name in messages.
CORE_SECTIONS = {"noise": "snr", "rt60": "gate"}
CORE_NAMES = {"noise": "a noise", "rt60": "an RT60"}

TAKE_SUFFIXES = {"phrase": "_phrase.wav", "noise": "_noise.wav", "clap": "_clap.wav"}

# The platform's ingest refuses a capture below 44.1 kHz (app/cores.py _door_gate) and
# makes every working copy at 48 kHz.
RATE_FLOOR = 44100
WORKING_RATE = 48000


def load_profile(name):
    """(noise_config, rt60_config) for a profile. None means the core's built-in default.

    Mirrors the platform's _resolve_profile: the core's own load_config reads
    configs/<core>/<name>.yaml.
    """
    if name not in PROFILES:
        raise ValueError(f"unknown profile {name!r}; M1 knows {', '.join(PROFILES)}")
    if name == "default":
        return None, None
    noise_config = noise_core.load_config(CORES_DIR / "configs" / "noise" / f"{name}.yaml")
    rt60_config = rt60_core.load_config(CORES_DIR / "configs" / "rt60" / f"{name}.yaml")
    return noise_config, rt60_config


def resolve_profile(core, choice):
    """(config, stamp) for one core's profile: a shipped name or the path of a user's yaml.

    config is what the core is called with: None for "default", as the platform passes
    None when no profile is set. stamp is the platform's AB-6 stamp, the profile's name
    and config_sha256 (the core's own hash of the config it runs), plus "shipped" or
    "yours".

    A user's yaml is read by the core's own load_config, merged over the core's built-in
    config. RecQC refuses it first unless it sets profile_name and carries the core's own
    section: a yaml with no name would be stamped with the built-in's name, and an RT60
    yaml loaded for the quiet room would run the noise defaults under the RT60 name.
    """
    module = CORE_MODULES[core]
    if choice in SHIPPED_PROFILES[core]:
        source = "shipped"
        config = (None if choice == "default"
                  else module.load_config(CORES_DIR / "configs" / core / f"{choice}.yaml"))
    else:
        source = "yours"
        try:
            raw = yaml.safe_load(Path(choice).read_text(encoding="utf-8"))
        except yaml.YAMLError as e:   # one line for the page, which shows the last line
            mark = getattr(e, "problem_mark", None)
            where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
            raise ValueError(f"the yaml does not parse{where}: {getattr(e, 'problem', None) or e}") from None
        if not isinstance(raw, dict):
            raise ValueError("the yaml must be a mapping of settings, as the shipped profiles are")
        if not raw.get("profile_name"):
            raise ValueError("the yaml must set profile_name, so each result names its profile")
        if not isinstance(raw.get(CORE_SECTIONS[core]), dict):
            raise ValueError(f"the yaml has no '{CORE_SECTIONS[core]}' section, "
                             f"so it is not {CORE_NAMES[core]} profile")
        config = module.load_config(choice)
    resolved = config if config is not None else module.load_config(None)
    stamp = {"profile_name": resolved.get("profile_name"),
             "config_sha256": module.config_sha256(resolved), "source": source}
    return config, stamp


def describe_profile(core, choice):
    """The page's view of a profile before any take: its stamp and the full config the
    core will run. Raises as resolve_profile does, so a bad yaml is refused on pick."""
    config, stamp = resolve_profile(core, choice)
    resolved = config if config is not None else CORE_MODULES[core].load_config(None)
    return to_plain({"stamp": stamp, "config": resolved})


def find_takes(folder):
    """The folder's phrase, noise and clap WAVs, found by file-name suffix (exactly one each)."""
    takes = {}
    for role, suffix in TAKE_SUFFIXES.items():
        found = sorted(p for p in Path(folder).iterdir() if p.name.endswith(suffix))
        if len(found) != 1:
            raise ValueError(f"{Path(folder).name}: expected one *{suffix}, found {len(found)}")
        takes[role] = found[0]
    return takes


def identity(wav):
    """(talent_id, language_code) from a <talent>_<lang>_... file name, as the corpus tools do."""
    parts = Path(wav).stem.split("_")
    if len(parts) >= 2 and parts[0].isdigit():
        return parts[0], parts[1]
    return "", ""


def to_plain(obj):
    """A JSON-safe copy: numpy scalars and arrays become Python numbers and lists, paths strings."""
    if isinstance(obj, dict):
        return {str(k): to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_plain(v) for v in obj]
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, Path):
        return str(obj)
    return obj


def stage_runner(out, folder):
    """A run(stage, fn) for one record: it runs fn, times it into out["timings"] and
    stores the result as out[stage]. An exception becomes {"error": ...} with the
    folder path stripped, so the two sides of the parity test can compare it."""
    def run(stage, fn):
        t0 = time.perf_counter()
        try:
            result, ok = fn(), True
        except Exception as e:  # noqa: BLE001 - recorded, not raised
            result, ok = {"error": f"{type(e).__name__}: {e}".replace(str(folder), "<folder>")}, False
        out["timings"][stage] = round(time.perf_counter() - t0, 3)
        out[stage] = result
        return result if ok else None
    return run


def working_copy(wav):
    """(the file the cores read, True if it was derived), by ingest_capture's rule: a
    48 kHz, 24-bit, mono take is its own working copy; anything else is derived next to
    it by the platform's _derive_working. (The platform counts an unknown subtype as
    24-bit; RecQC's takes are always PCM, so the subtype is checked directly.)"""
    wav = Path(wav)
    info = sf.info(str(wav))
    if info.samplerate == WORKING_RATE and info.subtype == "PCM_24" and info.channels == 1:
        return wav, False
    dest = wav.with_name(f"{wav.stem}_48k.wav")
    platform_ingest._derive_working(wav, dest, "wav")
    return dest, True


def level_gate(level):
    """The level stage's [verdict, reason]: the platform's gate, and a PASS with no
    measurable speech level becomes ERROR, as on the platform."""
    verdict, reason = spc._gate_level_check(level)
    if verdict == "PASS" and level.get("speech_level_dba") is None:
        verdict, reason = "ERROR", "speech_level_dba unmeasurable; cannot run noise SNR"
    return [verdict, reason]


def run_folder(folder, profile="default"):
    """Run every stage on one folder of takes. Returns a JSON-safe dict."""
    folder = Path(folder)
    takes = find_takes(folder)
    talent_id, language_code = identity(takes["phrase"])
    ids = {"talent_id": talent_id, "language_code": language_code}
    noise_config, rt60_config = load_profile(profile)
    out = {"folder": folder.name, "profile": profile, "talent_id": talent_id,
           "language_code": language_code, "timings": {}}
    run = stage_runner(out, folder)

    # The cores read the working copies; the format check below reads the takes as captured.
    copies = {role: working_copy(path) for role, path in takes.items()}
    work = {role: path for role, (path, _) in copies.items()}
    out["working_copy"] = {role: {"file": path.name, "derived": derived}
                           for role, (path, derived) in copies.items()}

    level = run("level", lambda: level_core.scan_phrase(
        work["phrase"], write_outputs=False, **ids))
    speech = level.get("speech_level_dba") if level else None
    if level:
        out["level_gate"] = level_gate(level)

    # Without a speech level the noise core would go looking for a phrase in the folder.
    if speech is None:
        out["noise"] = {"error": "skipped: no speech level from the level check"}
        noise = None
    else:
        noise = run("noise", lambda: noise_core.scan_noise_profile(
            work["noise"], modality="session", speech_level_dba=float(speech),
            config=noise_config, write_outputs=True,
            log_path=folder / f"{takes['noise'].stem}_log.jsonl", **ids))
    if noise:
        out["noise_gate"] = list(spc._gate_noise(noise))

    # RT60's noise profile: the trimmed _np.wav when the noise core wrote one, else the
    # noise working copy (the screening pipeline's rule, which made the corpus records).
    np_wav = Path((noise or {}).get("output_wav") or work["noise"])
    rt60 = run("rt60", lambda: rt60_core.analyze_file(
        work["clap"], config=rt60_config, noise_profile_path=np_wav, **ids))
    if rt60:
        out["rt60_gate"] = list(spc._gate_rt60(rt60, borderline_pass=True))

    run("provenance", lambda: {role: provenance.verify_provenance(path).as_dict()
                               for role, path in takes.items()})
    return to_plain(out)


def run_level_take(wav, bypass="verified"):
    """Score the level take the page captured. Returns a JSON-safe dict.

    The page wrote `wav` with the platform's encodeWav24, at the capture rate. As in the
    platform's ingest_capture: the 44.1 kHz floor, then the format verdict on the take
    as captured, then the 48 kHz working copy, then the level core and its gate on that
    copy. `bypass` is the page's mic-processing readback, "verified" or "unverified"; a
    take with processing active never gets here (design note D4). The format ceiling is
    ceiling_from_provenance (4 only for confirmed_native and lossless), held at 3 when
    the readback is unverified (design note section 5).
    """
    wav = Path(wav)
    info = sf.info(str(wav))
    out = {"take": wav.name, "bypass": bypass, "timings": {},
           "capture": {"sample_rate": info.samplerate, "subtype": info.subtype,
                       "channels": info.channels, "duration": info.duration}}
    if info.samplerate < RATE_FLOOR:
        # The platform's door-gate message, word for word.
        out["refused"] = f"Sample rate {info.samplerate} Hz is below the 44.1 kHz minimum."
        return to_plain(out)
    run = stage_runner(out, wav.parent)

    prov = run("provenance", lambda: provenance.verify_provenance(wav).as_dict())
    if prov:
        ceiling = provenance.ceiling_from_provenance(prov)
        out["format_ceiling"] = min(ceiling, 3) if bypass != "verified" else ceiling

    def make_working_copy():
        path, derived = working_copy(wav)
        return {"file": path, "derived": derived}

    copy = run("working_copy", make_working_copy)
    if copy:
        level = run("level", lambda: level_core.scan_phrase(copy["file"], write_outputs=False))
        if level:
            out["level_gate"] = level_gate(level)
    return to_plain(out)


def run_noise_take(wav, speech_level_dba, profile="platform_band"):
    """Score the room-tone take the page captured. Returns a JSON-safe dict.

    The platform's quiet-room stage (app/screening.py analyze_noise) in its order: the
    44.1 kHz floor, the 48 kHz working copy, then scan_noise_profile in session modality
    with the speech level the level take measured, and the platform's noise gate. The
    page asks only after this run's level gate passed, as the platform refuses the stage
    until then. There is no format verdict here: the platform takes it from one take,
    and RecQC takes it from the level take (Rick's ruling, 2026-10-21).

    `profile` is a shipped noise profile or the path of the user's yaml. On a PASS the
    core writes the room tone, trimmed to its longest clean run, as <take>_np.wav: the
    file RT60 reads and "Save noise profile" offers, named with its length under "np".
    A REJECT writes no WAV, as the core decides.
    """
    wav = Path(wav)
    info = sf.info(str(wav))
    out = {"take": wav.name, "timings": {},
           "capture": {"sample_rate": info.samplerate, "subtype": info.subtype,
                       "channels": info.channels, "duration": info.duration}}
    if info.samplerate < RATE_FLOOR:
        out["refused"] = f"Sample rate {info.samplerate} Hz is below the 44.1 kHz minimum."
        return to_plain(out)
    try:
        config, out["profile"] = resolve_profile("noise", profile)
    except Exception as e:  # noqa: BLE001 - reported, not raised
        out["profile"] = {"error": f"{type(e).__name__}: {e}"}
        return to_plain(out)
    run = stage_runner(out, wav.parent)

    def make_working_copy():
        path, derived = working_copy(wav)
        return {"file": path, "derived": derived}

    copy = run("working_copy", make_working_copy)
    if copy:
        noise = run("noise", lambda: noise_core.scan_noise_profile(
            copy["file"], modality="session", speech_level_dba=float(speech_level_dba),
            config=config, write_outputs=True, log_path=wav.parent / f"{wav.stem}_log.jsonl"))
        if noise:
            out["noise_gate"] = list(spc._gate_noise(noise))
            # Whether room reflection runs for this room: the platform skips it in the
            # lo-fi band (noise WARN), where the noise term alone caps the tier at 2.
            out["rt60_runs"] = platform_stages.rt60_in_band(noise.get("disposition"))
            if noise.get("output_wav"):
                out["np"] = {"file": noise["output_wav"],
                             "duration": sf.info(noise["output_wav"]).duration}
    return to_plain(out)


def run_clap_take(wavs, np_wav=None, profile="platform_band"):
    """Score the room-reflection take the page captured. Returns a JSON-safe dict.

    The platform's clap stage (app/screening.py analyze_clap) in its order: the 44.1 kHz
    floor and the 48 kHz working copy, then analyze_file with the quiet room's trimmed
    room tone as its noise profile, the platform's RT60 gate (borderline passes) and
    clap_collection_state, which says whether the platform would ask for more claps.

    `wavs` lists the takes in recording order: one, or more when the page asked for more
    claps. As the platform's append, each take's working copy is joined to the ones
    before it by concat_clap_takes and the whole is analysed again; the claps are never
    counted take by take. `np_wav` is the _np.wav the quiet room kept, or None, and then
    the core estimates the noise from each decay's tail. `profile` is a shipped RT60
    profile or the path of the user's yaml.

    The page asks only after the quiet-room gate passed and only when rt60_in_band says
    the stage runs, as the platform refuses it otherwise.
    """
    wavs = [Path(w) for w in wavs]
    folder = wavs[0].parent
    out = {"takes": [w.name for w in wavs], "timings": {}, "capture": []}
    for wav in wavs:
        info = sf.info(str(wav))
        out["capture"].append({"sample_rate": info.samplerate, "subtype": info.subtype,
                               "channels": info.channels, "duration": info.duration})
        if info.samplerate < RATE_FLOOR:
            out["refused"] = f"Sample rate {info.samplerate} Hz is below the 44.1 kHz minimum."
            return to_plain(out)
    try:
        config, out["profile"] = resolve_profile("rt60", profile)
    except Exception as e:  # noqa: BLE001 - reported, not raised
        out["profile"] = {"error": f"{type(e).__name__}: {e}"}
        return to_plain(out)
    # The thresholds this take is scored against, for the margins.
    resolved = config if config is not None else rt60_core.load_config(None)
    out["thresholds"] = {"gate": resolved["gate"], "r_squared_min": resolved["quality"]["r_squared_min"]}
    run = stage_runner(out, folder)

    def make_working_copy():
        copies = [working_copy(w) for w in wavs]
        path = copies[0][0]
        for i, (addition, _) in enumerate(copies[1:], start=1):
            path = platform_clap.concat_clap_takes(path, addition, folder / f"{wavs[0].stem}_joined{i}.wav")
        return {"file": path, "derived": any(derived for _, derived in copies), "takes_joined": len(copies)}

    copy = run("working_copy", make_working_copy)
    if not copy:
        return to_plain(out)
    t0 = time.perf_counter()
    try:
        rec = rt60_core.analyze_file(copy["file"], config=config, noise_profile_path=np_wav)
    except IndexError as e:
        # The platform's translation of a known core edge case (Lundeby truncation finds
        # no noise crossing): the clapper stood too far back. Its words, verbatim.
        rec, reason = None, ("too far back — take 1 step forward and try again"
                             if "out of bounds" in str(e) else f"analyze_file raised: {e}")
    except Exception as e:  # noqa: BLE001 - the platform records it as the stage's ERROR
        rec, reason = None, f"analyze_file raised: {e}"
    out["timings"]["rt60"] = round(time.perf_counter() - t0, 3)
    if rec is None:
        out["rt60_gate"] = ["ERROR", reason.replace(str(folder), "<folder>")]
    else:
        out["rt60"] = rec
        out["rt60_gate"] = list(spc._gate_rt60(rec, borderline_pass=True))
        out["clap_collection"] = platform_clap.clap_collection_state(rec, *out["rt60_gate"])
    # Which of the platform's two refusals this is: too live (measured; more claps cannot
    # help) or unmeasured (more claps can), with the platform's words for each.
    kind = platform_clap.rt60_reject_kind(*out["rt60_gate"])
    if kind:
        out["refusal"] = {"kind": kind, "copy": platform_clap.RT60_REJECT_COPY[kind]}
    return to_plain(out)


# ---- The ceiling card and the margins (design note sections 4.7 and 9) ----

def gate_of(result, gate_key, record_key):
    """A stage's [verdict, reason] from the page's result for it, also when the stage
    never reached its gate: a refused take is a FAIL; a profile that did not load, a
    core error, or an analysis that failed in the worker ({"error": ...}) an ERROR."""
    if result.get("error"):
        return ["ERROR", result["error"]]
    if result.get("refused"):
        return ["FAIL", result["refused"]]
    if (result.get("profile") or {}).get("error"):
        return ["ERROR", result["profile"]["error"]]
    if result.get(gate_key):
        return list(result[gate_key])
    error = (result.get(record_key) or result.get("working_copy") or {}).get("error")
    return ["ERROR", error or "the stage did not reach its gate"]


def tier_ceiling(level, noise, clap, capture):
    """The audio tier ceiling as the platform's tier matrix computes it (audition_reviews
    _matrix_audio_ceiling): min(format, environment, device). The environment cap always
    applies here (Rick 2026-10-22, M4 ruling R2); on the platform it is the deploy-time
    env_tier_cap_enabled, on by default.

    level, noise and clap are the page's results from run_level_take, run_noise_take and
    run_clap_take, or None for a stage not yet run. capture is the page's mic readback,
    of which only is_mobile is read here.

    The stages are walked in the platform's order, room reflection only when rt60_in_band
    says it runs for this room. The first not yet run is `next`. The first that failed
    its gate is `failed`, and then there is no ceiling, only the cap the take would have
    had (`would_be`, design note D3). Components not yet known are None.
    """
    noise_rec = (noise or {}).get("noise") or {}
    noise_disp = noise_rec.get("disposition") if noise else None
    rt60_runs = platform_stages.rt60_in_band(noise_disp)
    stages = [("level", level, "level_gate", "level"), ("noise", noise, "noise_gate", "noise")]
    if rt60_runs:
        stages.append(("clap", clap, "rt60_gate", "rt60"))

    out = {"next": None, "failed": None, "ceiling": None, "name": None, "would_be": None,
           "rt60_runs": rt60_runs}
    for stage, result, gate_key, record_key in stages:
        if result is None:
            out["next"] = stage
            break
        verdict, reason = gate_of(result, gate_key, record_key)
        if verdict != "PASS":
            out["failed"] = {"stage": stage, "verdict": verdict, "reason": reason}
            break

    # Format: the level take's (already held at 3 when the mic check is unverified); a
    # take whose format check errored gets the platform's fail-safe for no provenance.
    fmt = None
    if level and not level.get("refused"):
        fmt = level.get("format_ceiling", provenance.ceiling_from_provenance(None))
    rt60_disp = ((clap or {}).get("rt60") or {}).get("disposition") if rt60_runs else None
    env = platform_ceiling._env_ceiling(noise_disp, rt60_disp)
    device = platform_ceiling._device_ceiling((capture or {}).get("is_mobile"))
    out["components"] = {
        # held: the unverified mic check lowered a format that would have reached 4.
        "format": {"cap": fmt, "verdict": ((level or {}).get("provenance") or {}).get("spectral_verdict"),
                   "held": bool(fmt is not None and level.get("bypass") != "verified"
                                and provenance.ceiling_from_provenance(level.get("provenance")) > fmt),
                   "failsafe": bool(fmt is not None and "format_ceiling" not in level)},
        "environment": {"cap": env, "noise": noise_disp, "rt60": rt60_disp,
                        "known": noise is not None and (not rt60_runs or clap is not None)},
        "device": {"cap": device, "is_mobile": (capture or {}).get("is_mobile")},
    }
    out["would_be"] = min(c for c in (fmt, env, device) if c is not None)
    if out["next"] is None and out["failed"] is None:
        out["ceiling"] = out["would_be"]
        out["name"] = tiers.AUDIO_TIERS[out["ceiling"]]
    out["would_be_name"] = tiers.AUDIO_TIERS[out["would_be"]]
    return out


def margins(value, edges, bands, band, unit):
    """The distances from value to the two edges of its band. bands are in order of
    rising value with edges[i] between bands[i] and bands[i + 1]; band is the one the
    core placed the value in. Each margin names the neighbouring band across that edge."""
    if value is None or band not in bands:
        return []
    i = bands.index(band)
    out = []
    if i > 0:
        out.append({"side": "above", "band": bands[i - 1], "by": value - edges[i - 1], "unit": unit})
    if i < len(edges):
        out.append({"side": "below", "band": bands[i + 1], "by": edges[i] - value, "unit": unit})
    return out


def metric_rows(level, noise, clap, capture):
    """One row per metric (design note section 9): the value and unit, the band it
    landed in, the margins to the selected profile's nearest thresholds and a status
    word (D6): measured, derived, estimated, reported or self-reported. The bands are the
    cores' own dispositions; the edges come from the thresholds each record carries."""
    rows = []

    def add(metric, value, unit, status, band=None, edges=None, bands=None, margin_unit=None, note=None):
        rows.append({"metric": metric, "value": value, "unit": unit, "status": status, "band": band,
                     "margins": margins(value, edges, bands, band, margin_unit or unit) if edges else [],
                     "note": note})

    capture = capture or {}
    if capture.get("bypass"):
        add("Mic check", capture["bypass"], "", "reported",
            note={flag: capture.get(flag) for flag in ("agc", "ns", "ec")})
    mobile = capture.get("is_mobile")
    add("Device class", "mobile" if mobile else "desktop" if mobile is False else "not reported", "",
        "self-reported")

    lv = (level or {}).get("level") or {}
    if lv and not lv.get("error"):
        th = lv["thresholds"]
        tp, sp = lv.get("true_peak_dbtp"), lv.get("sample_peak_dbfs")
        # The core's peak disposition joins two tests: CLIP on true peak, HOT on sample
        # peak. Each row shows its own test, classified as the core classifies it.
        add("True peak", tp, "dBTP", "measured",
            band=None if tp is None else "CLIP" if tp >= th["clip_at_dbtp"] else "under CLIP",
            edges=[th["clip_at_dbtp"]], bands=["under CLIP", "CLIP"], margin_unit="dB")
        add("Sample peak", sp, "dBFS", "measured",
            band=None if sp is None else "HOT" if sp > th["hot_above_dbfs"] else "not HOT",
            edges=[th["hot_above_dbfs"]], bands=["not HOT", "HOT"], margin_unit="dB")
        add("Integrated loudness", lv.get("integrated_lufs"), "LUFS", "measured",
            band=lv.get("lufs_disposition"), edges=[th["too_quiet_below_lufs"], th["quiet_below_lufs"]],
            bands=["TOO_QUIET", "QUIET", "PASS"], margin_unit="LU")
        add("Speech level (A-weighted)", lv.get("speech_level_dba"), "dBFS", "measured")
        add("Crest factor", lv.get("crest_factor_db"), "dB", "derived")

    nz = (noise or {}).get("noise") or {}
    if nz and not nz.get("error"):
        th = nz["thresholds"]
        add("Noise floor (A-weighted)", nz.get("noise_floor_dba"), "dBFS", "measured")
        add("SNR (A-weighted)", nz.get("snr_db"), "dB", "derived", band=nz.get("disposition"),
            edges=[th["reject_below"], th["warn_below"], th["caution_below"]],
            bands=["REJECT", "WARN", "CAUTION", "PASS"])

    # A clap result counts only while rt60_in_band says room reflection runs for this
    # room: a re-scored quiet room can move it into the lo-fi band.
    in_band = platform_stages.rt60_in_band(nz.get("disposition") if noise else None)
    rt = ((clap or {}).get("rt60") or {}) if in_band else {}
    if rt and clap.get("thresholds"):
        gate, summary = clap["thresholds"]["gate"], rt.get("summary") or {}
        if gate.get("production_below") is not None:
            edges = [gate["pass_below"], gate["production_below"], gate["reject_above"]]
            bands = ["pass", "production", "borderline", "reject"]
        else:
            edges, bands = [gate["pass_below"], gate["reject_above"]], ["pass", "borderline", "reject"]
        usable = [c for c in rt.get("claps") or []
                  if c.get("valid") and (c.get("rt60_per_band") or {}).get("1000") is not None]
        r2 = [c["fit_quality"]["1000"]["r_squared"] for c in usable]
        note = {"detected": summary.get("n_claps_total"), "valid": summary.get("n_claps_valid"),
                "usable": len(usable), "r_squared": [min(r2), max(r2)] if r2 else None,
                "r_squared_min": clap["thresholds"]["r_squared_min"], "dry_room": rt.get("dry_room")}
        add("RT60 at 1 kHz (T20)", summary.get("rt60_gate_metric"), "s", "estimated",
            band=rt.get("disposition"), edges=edges, bands=bands, note=note)
    return rows


def summary(level=None, noise=None, clap=None, capture=None):
    """The page's ceiling card and per-metric rows from its results so far."""
    return to_plain({"ceiling": tier_ceiling(level, noise, clap, capture),
                     "metrics": metric_rows(level, noise, clap, capture)})


def run_page_flow(folder, noise_profile="platform_band", rt60_profile="platform_band"):
    """The page's flow over one folder of takes, with no browser: the phrase through
    run_level_take; the noise take through run_noise_take with that take's speech level,
    only if the level gate passed; the clap take through run_clap_take with the room tone
    the quiet room kept, only if the quiet-room gate passed and rt60_in_band says room
    reflection runs; then the summary of what ran, as from a desktop with a verified mic
    check. Returns a JSON-safe dict."""
    folder = Path(folder)
    takes = find_takes(folder)
    out = {"folder": folder.name, "level": run_level_take(takes["phrase"])}
    if (out["level"].get("level_gate") or [None])[0] == "PASS":
        out["noise"] = run_noise_take(takes["noise"], out["level"]["level"]["speech_level_dba"],
                                      noise_profile)
    else:
        out["noise"] = {"skipped": "the level gate did not pass"}
    if (out["noise"].get("noise_gate") or [None])[0] != "PASS":
        out["clap"] = {"skipped": "the quiet-room gate did not pass"}
    elif not out["noise"]["rt60_runs"]:
        out["clap"] = {"skipped": "the lo-fi band: room reflection does not run"}
    else:
        out["clap"] = run_clap_take([takes["clap"]], (out["noise"].get("np") or {}).get("file"),
                                    rt60_profile)
    ran = {stage: None if "skipped" in out[stage] else out[stage] for stage in ("noise", "clap")}
    out["summary"] = summary(out["level"], ran["noise"], ran["clap"],
                             {"bypass": "verified", "agc": False, "ns": False, "ec": False,
                              "is_mobile": False})
    return out


def take_folders(root):
    """The subfolders of root that hold takes, in name order."""
    return sorted(p for p in Path(root).iterdir()
                  if p.is_dir() and any(p.glob("*" + TAKE_SUFFIXES["phrase"])))


def run_corpus(root, profile="default"):
    """run_folder on every subfolder of root that holds takes, in name order."""
    return [run_folder(f, profile) for f in take_folders(root)]


def run_page_corpus(root, noise_profile="platform_band", rt60_profile="platform_band"):
    """run_page_flow on every subfolder of root that holds takes, in name order."""
    return [run_page_flow(f, noise_profile, rt60_profile) for f in take_folders(root)]


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        sys.exit(2)
    target = Path(sys.argv[1])
    profile = sys.argv[2] if len(sys.argv) == 3 else "default"
    if any(target.glob("*" + TAKE_SUFFIXES["phrase"])):
        result = run_folder(target, profile)
    else:
        result = run_corpus(target, profile)
    print(json.dumps(result, indent=2))
