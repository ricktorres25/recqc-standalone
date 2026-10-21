"""
recqc_runner.py - RecQC's thin orchestration over the vendored platform cores.

RecQC code, not vendored. It calls the cores the way the live platform does
(MAD/Talent Platform app/screening.py and app/cores.py), so a take scores here as it
would on the platform:

    level   scan_phrase(wav, write_outputs=False); a PASS with no measurable speech
            level becomes ERROR, as on the platform
    noise   scan_noise_profile(wav, modality="session", speech_level_dba=<from level>,
            config=<profile>, write_outputs=True, log_path=<per take>); on PASS the
            core writes <take>_np.wav, the trimmed room tone RT60 uses
    rt60    analyze_file(wav, config=<profile>, noise_profile_path=<the _np.wav>)
    gates   screening_pipeline_core._gate_level_check, _gate_noise and
            _gate_rt60(borderline_pass=True), the functions the platform calls
    format  provenance.verify_provenance on each take

M1 scope: one folder of takes (a phrase, a noise and a clap WAV), no browser and no
mic. Every stage that can run does run, for coverage; stopping at the first failed
gate comes with the M2-M4 flow. The results are plain JSON-safe dicts, so the same
function runs in CPython and in Pyodide and the parity test can diff the two.

Usage:  python3 py/recqc_runner.py <folder of takes, or a folder of such folders> [profile]
"""
import json
import sys
import time
from pathlib import Path

import numpy as np

# Importing the cores package runs the start check before any vendored module loads.
from cores import CORES_DIR
from cores import level_check_phrase_core as level_core
from cores import noise_profile_scanner_core as noise_core
from cores import provenance
from cores import rt60_analyzer_core_v5 as rt60_core
from cores import screening_pipeline_core as spc

# M1 profiles. "default" is each core's built-in config (what the stored corpus
# records used); "platform_band" is the policy the live platform runs.
PROFILES = ("default", "platform_band")

TAKE_SUFFIXES = {"phrase": "_phrase.wav", "noise": "_noise.wav", "clap": "_clap.wav"}


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


def run_folder(folder, profile="default"):
    """Run every stage on one folder of takes. Returns a JSON-safe dict."""
    folder = Path(folder)
    takes = find_takes(folder)
    talent_id, language_code = identity(takes["phrase"])
    ids = {"talent_id": talent_id, "language_code": language_code}
    noise_config, rt60_config = load_profile(profile)
    out = {"folder": folder.name, "profile": profile, "talent_id": talent_id,
           "language_code": language_code, "timings": {}}

    def run(stage, fn):
        """Run one stage, timing it. An exception becomes {"error": ...} with the
        folder path stripped, so the two sides of the parity test can compare it."""
        t0 = time.perf_counter()
        try:
            result, ok = fn(), True
        except Exception as e:  # noqa: BLE001 - recorded, not raised
            result, ok = {"error": f"{type(e).__name__}: {e}".replace(str(folder), "<folder>")}, False
        out["timings"][stage] = round(time.perf_counter() - t0, 3)
        out[stage] = result
        return result if ok else None

    level = run("level", lambda: level_core.scan_phrase(
        takes["phrase"], write_outputs=False, **ids))
    speech = level.get("speech_level_dba") if level else None
    if level:
        verdict, reason = spc._gate_level_check(level)
        if verdict == "PASS" and speech is None:
            verdict, reason = "ERROR", "speech_level_dba unmeasurable; cannot run noise SNR"
        out["level_gate"] = [verdict, reason]

    # Without a speech level the noise core would go looking for a phrase in the folder.
    if speech is None:
        out["noise"] = {"error": "skipped: no speech level from the level check"}
        noise = None
    else:
        noise = run("noise", lambda: noise_core.scan_noise_profile(
            takes["noise"], modality="session", speech_level_dba=float(speech),
            config=noise_config, write_outputs=True,
            log_path=folder / f"{takes['noise'].stem}_log.jsonl", **ids))
    if noise:
        out["noise_gate"] = list(spc._gate_noise(noise))

    # RT60's noise profile: the trimmed _np.wav when the noise core wrote one, else the
    # raw noise take (the screening pipeline's rule, which made the corpus records).
    np_wav = Path((noise or {}).get("output_wav") or takes["noise"])
    rt60 = run("rt60", lambda: rt60_core.analyze_file(
        takes["clap"], config=rt60_config, noise_profile_path=np_wav, **ids))
    if rt60:
        out["rt60_gate"] = list(spc._gate_rt60(rt60, borderline_pass=True))

    run("provenance", lambda: {role: provenance.verify_provenance(path).as_dict()
                               for role, path in takes.items()})
    return to_plain(out)


def run_corpus(root, profile="default"):
    """run_folder on every subfolder of root that holds takes, in name order."""
    folders = sorted(p for p in Path(root).iterdir()
                     if p.is_dir() and any(p.glob("*" + TAKE_SUFFIXES["phrase"])))
    return [run_folder(f, profile) for f in folders]


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
