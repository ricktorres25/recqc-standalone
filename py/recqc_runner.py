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

The entry points. run_folder scores one folder of takes (a phrase, a noise and a clap
WAV) with no browser and no mic; every stage that can run does run, for coverage, and
the parity test diffs its records between CPython and Pyodide. The page's takes go
through run_level_take (M2) and run_noise_take (M3), one call per take, in the
platform's stage order: the quiet room runs only after the level gate passed.
run_page_flow runs that order over a folder of takes, so the parity test covers the
page's path too. The results are plain JSON-safe dicts, so the same functions run in
CPython and in Pyodide.

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
from cores import platform_ingest
from cores import provenance
from cores import rt60_analyzer_core_v5 as rt60_core
from cores import screening_pipeline_core as spc

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
            if noise.get("output_wav"):
                out["np"] = {"file": noise["output_wav"],
                             "duration": sf.info(noise["output_wav"]).duration}
    return to_plain(out)


def run_page_flow(folder, noise_profile="platform_band"):
    """The page's flow over one folder of takes, with no browser: the phrase through
    run_level_take, then the noise take through run_noise_take with that take's speech
    level, only if the level gate passed. Returns a JSON-safe dict."""
    folder = Path(folder)
    takes = find_takes(folder)
    out = {"folder": folder.name, "level": run_level_take(takes["phrase"])}
    if (out["level"].get("level_gate") or [None])[0] == "PASS":
        out["noise"] = run_noise_take(takes["noise"], out["level"]["level"]["speech_level_dba"],
                                      noise_profile)
    else:
        out["noise"] = {"skipped": "the level gate did not pass"}
    return out


def take_folders(root):
    """The subfolders of root that hold takes, in name order."""
    return sorted(p for p in Path(root).iterdir()
                  if p.is_dir() and any(p.glob("*" + TAKE_SUFFIXES["phrase"])))


def run_corpus(root, profile="default"):
    """run_folder on every subfolder of root that holds takes, in name order."""
    return [run_folder(f, profile) for f in take_folders(root)]


def run_page_corpus(root, noise_profile="platform_band"):
    """run_page_flow on every subfolder of root that holds takes, in name order."""
    return [run_page_flow(f, noise_profile) for f in take_folders(root)]


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
