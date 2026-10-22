#!/usr/bin/env python3
# test_profiles.py - the scoring profiles and the quiet-room take (M3), without a corpus.
#
#   R1  every shipped profile resolves and is stamped with its name and config_sha256;
#       the noise core's Default and Platform Band differ only where the yaml says
#   R2  the RT60 core's built-in config is Strict, the reason RT60 lists Strict alone
#   R3  a user's yaml is merged over the core's built-in config and stamped "yours"
#   R4  a user's yaml is refused when it has no profile_name, lacks the core's section,
#       is not a mapping, or does not parse
#   R5  run_noise_take on synthetic takes: a quiet room passes and keeps its room tone,
#       stamped with the profile's hash as the core's record is; a loud room is rejected
#       and keeps none; a 44.1 kHz take is scored on a derived working copy; a take below
#       44.1 kHz is refused with the platform's message; a bad profile stops the scan
#
# Every check runs its near miss (builds:grader-negative-control) where one applies: a
# case that differs from a passing one only in the property being checked.
#
# Run:   python3 tests/test_profiles.py
# Exit:  0 pass, 1 fail
import sys
sys.dont_write_bytecode = True   # leave no __pycache__ in py/

import json
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "py"))
import recqc_runner as runner  # noqa: E402  (importing it runs the cores' start check)

FIXTURE = REPO / "tests" / "parity" / "fixture_noise_profile.yaml"
RT60_CONFIGS = runner.CORES_DIR / "configs" / "rt60"

statuses = []


def result(ok, text):
    statuses.append("PASS" if ok else "FAIL")
    print(f"{'PASS' if ok else 'FAIL':8s} {text}")


def refused(core, path):
    """The refusal message for a profile, or None when it was accepted."""
    try:
        runner.resolve_profile(core, str(path))
    except Exception as e:  # noqa: BLE001 - the message is the result
        return f"{type(e).__name__}: {e}"
    return None


def noise_take(folder, name, rate, rms_dbfs, seconds=5.0):
    """A steady white-noise take at a broadband RMS level, written as 24-bit mono."""
    rng = np.random.default_rng(7)
    x = rng.standard_normal(int(rate * seconds)) * 10 ** (rms_dbfs / 20)
    path = Path(folder) / name
    sf.write(str(path), x, rate, subtype="PCM_24")
    return path


def main():
    noise_core, rt60_core = runner.noise_core, runner.rt60_core

    # R1
    for core, choices in runner.SHIPPED_PROFILES.items():
        for choice in choices:
            config, stamp = runner.resolve_profile(core, choice)
            described = runner.describe_profile(core, choice)
            result(stamp["source"] == "shipped" and stamp["profile_name"] and len(stamp["config_sha256"]) == 64
                   and described["stamp"] == stamp and json.dumps(described),
                   f"R1 {core} {choice}: {stamp['profile_name']}, sha256 {stamp['config_sha256'][:12]}")
    default_cfg = noise_core.load_config(None)
    band_cfg, _ = runner.resolve_profile("noise", "platform_band")
    moved = {k for k in default_cfg["snr"] if default_cfg["snr"][k] != band_cfg["snr"][k]}
    result(moved == {"reject_below"} and band_cfg["snr"]["reject_below"] == 20.0
           and {k: v for k, v in default_cfg.items() if k not in ("snr", "profile_name", "config_version")}
           == {k: v for k, v in band_cfg.items() if k not in ("snr", "profile_name", "config_version")},
           "R1 noise Platform Band moves only snr.reject_below (38 -> 20) from Default")
    result(runner.resolve_profile("noise", "default")[0] is None,
           "R1 noise Default runs as config=None, as the platform runs an empty setting")
    result(runner.resolve_profile("noise", "default")[1]["config_sha256"] != runner.resolve_profile("noise", "platform_band")[1]["config_sha256"],
           "R1 near miss: Default and Platform Band carry different hashes")

    # R2
    builtin = rt60_core.config_sha256(rt60_core.load_config(None))
    result(builtin == runner.resolve_profile("rt60", "strict")[1]["config_sha256"]
           and rt60_core.load_config(None)["profile_name"] == "Strict",
           "R2 the RT60 core's built-in config is Strict, hash for hash")
    result(builtin != runner.resolve_profile("rt60", "forgiving")[1]["config_sha256"],
           "R2 near miss: the built-in config is not Forgiving")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # R3
        config, stamp = runner.resolve_profile("noise", str(FIXTURE))
        result(stamp["source"] == "yours" and stamp["profile_name"] == "Parity fixture"
               and config["snr"]["warn_below"] == 42.0 and config["impulse"] == default_cfg["impulse"]
               and stamp["config_sha256"] == noise_core.config_sha256(config),
               "R3 the fixture yaml is merged over the noise core's Default and stamped as yours")

        # R4: each refused yaml next to its near miss, the same yaml with the gap filled.
        cases = {
            "no profile_name": ("noise", "snr:\n  warn_below: 40.0\n",
                                "profile_name: Mine\nsnr:\n  warn_below: 40.0\n"),
            "an RT60 yaml loaded for the quiet room": ("noise", (RT60_CONFIGS / "forgiving.yaml").read_text(),
                                                       (RT60_CONFIGS / "forgiving.yaml").read_text() + "\nsnr:\n  warn_below: 40.0\n"),
            "a noise yaml loaded for RT60": ("rt60", FIXTURE.read_text(),
                                             FIXTURE.read_text() + "\ngate:\n  reject_above: 0.9\n"),
            "a list": ("noise", "- profile_name: Mine\n", "profile_name: Mine\nsnr: {}\n"),
            "broken syntax": ("noise", "profile_name: Mine\nsnr: [warn_below: 40\n",
                              "profile_name: Mine\nsnr: {warn_below: 40}\n"),
        }
        for i, (what, (core, bad, good)) in enumerate(cases.items()):
            bad_path, good_path = tmp / f"bad{i}.yaml", tmp / f"good{i}.yaml"
            bad_path.write_text(bad)
            good_path.write_text(good)
            message = refused(core, bad_path)
            result(message is not None, f"R4 refused, {what}: {message}")
            result(refused(core, good_path) is None, f"R4 near miss accepted, {what} with the gap filled")

        # R5
        take = noise_take(tmp, "quiet_noise.wav", 48000, -90.0)
        out = runner.run_noise_take(take, -30.0, "platform_band")
        rec = out["noise"]
        result(out["noise_gate"] == ["PASS", None] and rec["verdict"] == "PASS"
               and out["profile"]["config_sha256"] == rec["thresholds"]["config_sha256"]
               and not out["working_copy"]["derived"] and Path(out["np"]["file"]).is_file()
               and abs(out["np"]["duration"] - 5.0) < 1e-6,
               f"R5 a quiet room passes ({rec['disposition']}, SNR {rec['snr_db']:.1f} dB) and keeps "
               f"{out['np']['duration']:.1f} s of room tone, stamped with the record's own hash")
        loud = runner.run_noise_take(noise_take(tmp, "loud_noise.wav", 48000, -40.0), -30.0, "platform_band")
        result(loud["noise_gate"] == ["FAIL", "snr_reject"] and "np" not in loud
               and loud["noise"]["output_wav"] is None,
               f"R5 a loud room is rejected (SNR {loud['noise']['snr_db']:.1f} dB) and keeps no room tone")
        at44 = runner.run_noise_take(noise_take(tmp, "quiet44_noise.wav", 44100, -90.0), -30.0, "default")
        result(at44["working_copy"]["derived"] and at44["noise"]["sample_rate"] == 48000
               and at44["profile"]["profile_name"] == "Default" and at44["noise_gate"][0] == "PASS",
               "R5 a 44.1 kHz take is scored on a 48 kHz working copy (soxr), under Default")
        low = runner.run_noise_take(noise_take(tmp, "low_noise.wav", 32000, -90.0), -30.0, "platform_band")
        result(low.get("refused") == "Sample rate 32000 Hz is below the 44.1 kHz minimum." and "noise" not in low,
               "R5 a 32 kHz take is refused with the platform's door-gate message")
        result("refused" not in at44, "R5 near miss: a 44.1 kHz take is not refused")
        (tmp / "nameless.yaml").write_text("snr:\n  warn_below: 40.0\n")
        bad = runner.run_noise_take(take, -30.0, str(tmp / "nameless.yaml"))
        result("error" in bad["profile"] and "noise" not in bad,
               f"R5 a refused profile stops the scan: {bad['profile'].get('error')}")

    print()
    print("RESULT: FAIL" if "FAIL" in statuses else "RESULT: PASS")
    return 1 if "FAIL" in statuses else 0


if __name__ == "__main__":
    sys.exit(main())
