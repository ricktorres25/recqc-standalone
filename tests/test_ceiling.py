#!/usr/bin/env python3
# test_ceiling.py - the tier ceiling and the per-metric margins (M4), without a corpus.
#
#   C1  the lo-fi band (Rick's M4 ruling R1, the platform's rt60_in_band): after a noise
#       WARN room reflection does not run, the ceiling is complete at 2, and a clap result
#       left from before is ignored; after a CAUTION it is still owed
#   C2  a gate that fails (level, quiet room, room reflection, a refused take, an analysis
#       that failed in the worker) gives no ceiling, only the cap the take would have had
#       (design note D3)
#   C3  the format cap: 4 only for a confirmed-native take; held at 3 by an unverified mic
#       check; the platform's fail-safe when the format check errored
#   C4  the environment and device caps, and the ceiling as the lowest of the three,
#       named by the platform's AUDIO_TIERS
#   C5  the margins: the design note's own example (SNR 51.2 dB, CAUTION, 3.2 dB above
#       WARN, 3.8 dB below PASS), each band edge as its core places it, RT60 in seconds,
#       a too-dry room with no number and so no margin
#
# The results are built by hand in the shapes run_level_take, run_noise_take and
# run_clap_take return; every check sits next to its near miss
# (builds:grader-negative-control).
#
# Run:   python3 tests/test_ceiling.py
# Exit:  0 pass, 1 fail
import sys
sys.dont_write_bytecode = True   # leave no __pycache__ in py/

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "py"))
import recqc_runner as runner  # noqa: E402  (importing it runs the cores' start check)

statuses = []
DESKTOP = {"bypass": "verified", "agc": False, "ns": False, "ec": False, "is_mobile": False}
NOISE_TH = {"name": "Platform Band", "reject_below": 20.0, "warn_below": 48.0, "caution_below": 55.0, "pass_above": 55.0}
RT60_TH = {"gate": {"pass_below": 0.25, "production_below": 0.55, "reject_above": 0.9, "min_valid_claps": 3,
                    "dry_room_pass": True}, "r_squared_min": 0.85}


def result(ok, text):
    statuses.append("PASS" if ok else "FAIL")
    print(f"{'PASS' if ok else 'FAIL':8s} {text}")


def level(gate="PASS", verdict="confirmed_native", lossless=True, bypass="verified", tp=-6.0, sp=-6.2, lufs=-28.0):
    prov = {"spectral_verdict": verdict, "lossless": lossless}
    fmt = runner.provenance.ceiling_from_provenance(prov)
    return {"bypass": bypass, "provenance": prov, "format_ceiling": min(fmt, 3) if bypass != "verified" else fmt,
            "level_gate": [gate, None if gate == "PASS" else "Peak: clipping detected"],
            "level": {"true_peak_dbtp": tp, "sample_peak_dbfs": sp, "integrated_lufs": lufs,
                      "lufs_disposition": "PASS" if lufs > -36 else "QUIET" if lufs >= -42 else "TOO_QUIET",
                      "speech_level_dba": -30.0, "crest_factor_db": 18.0,
                      "thresholds": {"clip_at_dbtp": 0.0, "hot_above_dbfs": -3.0,
                                     "too_quiet_below_lufs": -42.0, "quiet_below_lufs": -36.0}}}


def noise(disposition="PASS", snr=58.0):
    gate = ["FAIL", "snr_reject"] if disposition == "REJECT" else ["PASS", None]
    return {"noise_gate": gate, "rt60_runs": runner.platform_stages.rt60_in_band(disposition),
            "noise": {"disposition": disposition, "snr_db": snr, "noise_floor_dba": -85.0, "thresholds": NOISE_TH}}


def clap(disposition="pass", metric=0.2, dry_room=None):
    verdict = {"pass": "PASS", "production": "PASS", "borderline": "PASS", "reject": "FAIL"}.get(disposition, "ERROR")
    claps = [{"valid": True, "rt60_per_band": {"1000": metric}, "fit_quality": {"1000": {"r_squared": 0.97}}}] * 6
    return {"thresholds": RT60_TH, "rt60_gate": [verdict, f"rt60_1khz_{metric}"],
            "rt60": {"disposition": disposition, "dry_room": dry_room, "claps": claps,
                     "summary": {"rt60_gate_metric": metric, "n_claps_total": 6, "n_claps_valid": 6}}}


def ceiling(lv, nz, cp, capture=DESKTOP):
    return runner.summary(lv, nz, cp, capture)["ceiling"]


def row(metrics, name):
    return next((m for m in metrics if m["metric"] == name), None)


def main():
    # C1
    c = ceiling(level(), noise("WARN", 45.0), None)
    result(c["next"] is None and c["ceiling"] == 2 and not c["rt60_runs"],
           "C1 noise WARN: room reflection does not run and the ceiling is complete at 2 (Lo-fi)")
    c = ceiling(level(), noise("CAUTION", 50.0), None)
    result(c["next"] == "clap" and c["ceiling"] is None and c["would_be"] == 3,
           "C1 near miss: after a CAUTION room reflection is still owed (so far at most 3)")
    s = runner.summary(level(), noise("WARN", 45.0), clap("reject", 1.2), DESKTOP)
    result(s["ceiling"]["ceiling"] == 2 and s["ceiling"]["failed"] is None
           and row(s["metrics"], "RT60 at 1 kHz (T20)") is None,
           "C1 a clap result left from before a WARN is ignored: no failure, no RT60 row")
    s = runner.summary(level(), noise("PASS"), clap("reject", 1.2), DESKTOP)
    result(s["ceiling"]["failed"]["stage"] == "clap" and row(s["metrics"], "RT60 at 1 kHz (T20)") is not None,
           "C1 near miss: the same clap result after a PASS fails room reflection and has its row")

    # C2
    cases = {
        "level FAIL": (level("FAIL"), None, None, "level", "FAIL", 4),
        "noise REJECT": (level(), noise("REJECT", 15.0), None, "noise", "FAIL", 2),
        "room reflection reject": (level(), noise("PASS"), clap("reject", 1.2), "clap", "FAIL", 2),
        "room reflection error": (level(), noise("CAUTION", 50.0), clap("error", None), "clap", "ERROR", 3),
        "a refused take": ({"refused": "Sample rate 32000 Hz is below the 44.1 kHz minimum."}, None, None, "level", "FAIL", 4),
        "an analysis that failed in the worker": (level(), {"error": "the analysis failed: MemoryError"}, None,
                                                  "noise", "ERROR", 4),
    }
    for what, (lv, nz, cp, stage, verdict, would) in cases.items():
        c = ceiling(lv, nz, cp)
        result(c["ceiling"] is None and c["failed"]["stage"] == stage and c["failed"]["verdict"] == verdict
               and c["would_be"] == would,
               f"C2 {what}: no ceiling, {stage} {verdict}, would have been at most {would}")
    c = ceiling(level(), noise("PASS"), clap("pass"))
    result(c["failed"] is None and c["ceiling"] == 4,
           "C2 near miss: every gate passed, so the ceiling is named (4)")

    # C3
    c = ceiling(level(), noise("PASS"), clap("pass"))["components"]["format"]
    result(c["cap"] == 4 and not c["held"], "C3 a confirmed-native, lossless take allows 4")
    c = ceiling(level(bypass="unverified"), noise("PASS"), clap("pass"))
    result(c["components"]["format"]["cap"] == 3 and c["components"]["format"]["held"] and c["ceiling"] == 3,
           "C3 the same take with the mic check unverified is held at 3")
    c = ceiling(level(verdict="indeterminate", bypass="unverified"), noise("PASS"), clap("pass"))["components"]["format"]
    result(c["cap"] == 3 and not c["held"],
           "C3 near miss: an indeterminate take (a 44.1 kHz chain) is 3 on its own, not held there by the mic check")
    lv = level()
    del lv["format_ceiling"]
    lv["provenance"] = {"error": "LibsndfileError"}
    c = ceiling(lv, noise("PASS"), clap("pass"))["components"]["format"]
    result(c["cap"] == 3 and c["failsafe"], "C3 a format check that errored takes the platform's fail-safe, 3")

    # C4
    for nd, rd, want in (("PASS", "pass", 4), ("CAUTION", "pass", 3), ("PASS", "production", 3),
                         ("PASS", "borderline", 2), ("CAUTION", "borderline", 2)):
        c = ceiling(level(), noise(nd, 58.0 if nd == "PASS" else 50.0), clap(rd, {"pass": 0.2, "production": 0.4,
                                                                                  "borderline": 0.7}[rd]))
        result(c["components"]["environment"]["cap"] == want and c["ceiling"] == want
               and c["name"] == runner.tiers.AUDIO_TIERS[want],
               f"C4 quiet room {nd} with room reflection {rd}: environment {want}, ceiling {want} ({c['name']})")
    c = ceiling(level(), noise("PASS"), clap("pass"), {**DESKTOP, "is_mobile": True})
    result(c["components"]["device"]["cap"] == 2 and c["ceiling"] == 2,
           "C4 a self-reported mobile device caps at 2 even in a perfect room")
    c = ceiling(level(), noise("PASS"), clap("pass"), {**DESKTOP, "is_mobile": None})
    result(c["components"]["device"]["cap"] == 4,
           "C4 near miss: a browser that does not report its device class is not capped")

    # C5
    m = row(runner.summary(level(), noise("CAUTION", 51.2), None, DESKTOP)["metrics"], "SNR (A-weighted)")
    text = ", ".join(f"{x['by']:.1f} {x['unit']} {x['side']} {x['band']}" for x in m["margins"])
    result(m["band"] == "CAUTION" and text == "3.2 dB above WARN, 3.8 dB below PASS" and m["status"] == "derived",
           f"C5 the design note's example: SNR 51.2 dB, CAUTION, {text}")
    m = row(runner.summary(level(), noise("PASS", 58.0), None, DESKTOP)["metrics"], "SNR (A-weighted)")
    result([x["band"] for x in m["margins"]] == ["CAUTION"],
           "C5 near miss: a PASS sits above CAUTION with no band beyond it")
    s = runner.summary(level(tp=-2.5, sp=-2.6, lufs=-36.0), None, None, DESKTOP)["metrics"]
    tp, sp, lu = row(s, "True peak"), row(s, "Sample peak"), row(s, "Integrated loudness")
    result(tp["band"] == "under CLIP" and abs(tp["margins"][0]["by"] - 2.5) < 1e-9
           and sp["band"] == "HOT" and sp["margins"][0]["band"] == "not HOT"
           and lu["band"] == "QUIET" and [x["band"] for x in lu["margins"]] == ["TOO_QUIET", "PASS"]
           and lu["margins"][1]["by"] == 0.0 and lu["margins"][1]["unit"] == "LU",
           "C5 the level edges as the core places them: -2.6 dBFS is HOT, exactly -36 LUFS is QUIET, 0.0 LU below PASS")
    s = runner.summary(level(), noise("PASS"), clap("production", 0.31), DESKTOP)["metrics"]
    rt = row(s, "RT60 at 1 kHz (T20)")
    result(rt["band"] == "production" and rt["unit"] == "s" and rt["status"] == "estimated"
           and [(x["side"], x["band"], round(x["by"], 3)) for x in rt["margins"]]
           == [("above", "pass", 0.06), ("below", "borderline", 0.24)] and rt["note"]["usable"] == 6,
           "C5 RT60 0.31 s, production: 0.060 s above pass, 0.240 s below borderline, from 6 usable claps")
    dry = clap("pass", None, {"agrees": True, "n_below_floor": 5, "n_claps_valid": 6})
    rt = row(runner.summary(level(), noise("PASS"), dry, DESKTOP)["metrics"], "RT60 at 1 kHz (T20)")
    result(rt["value"] is None and rt["margins"] == [] and rt["note"]["dry_room"]["agrees"],
           "C5 a room too dry to measure passes with no number, so no margin")
    statuses_by_metric = {m["metric"]: m["status"] for m in s}
    result(statuses_by_metric.get("Mic check") == "reported" and statuses_by_metric.get("Device class") == "self-reported"
           and statuses_by_metric.get("Noise floor (A-weighted)") == "measured"
           and statuses_by_metric.get("Crest factor") == "derived",
           "C5 the status words (D6): reported, self-reported, measured, derived")

    print()
    print("RESULT: FAIL" if "FAIL" in statuses else "RESULT: PASS")
    return 1 if "FAIL" in statuses else 0


if __name__ == "__main__":
    sys.exit(main())
