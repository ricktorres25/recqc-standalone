# RecQC Standalone: design note

**Status:** approved by Rick 2026-10-20 (gate 1 of the build). Written 2026-10-20 (session `s-2026-10-20-code-audio-pipeline-tooling-2`, task `t-recqc-standalone`) from the platform code as read that day. Rulings: career-planning loop `recqc-standalone` and `career-planning:platform-ownership`, both 2026-10-20.

## 1. What it is

The platform's Rec QC, audio side only, run in the browser on the user's machine: level check, quiet room, room reflection. Output: the **audio tier ceiling** `min(format, environment, device)`, every component, disposition and number shown. No linguistics, nothing uploaded, no server. Against audiolab.tools/voice-ai-check (measurements, no live mic, no policy) this is a protocol plus a policy, borrowing its per-metric status, margin and does-not-say notes.

## 2. Form factor and run modes

Static client-side web app, one folder, no backend, and the folder is the deploy artifact. Two user-facing paths: **Hosted**, GitHub Pages from the public repo, forkable; and **Installed**, the PWA installed once from the hosted page, which then opens from the Dock, works with the network off, and runs from Chrome's cache with no server anywhere. **Self-hosted** is one line for forks, intranets and machines that never go online: serve the folder with any static server (for example `python3 -m http.server 8000` in the folder, then `http://localhost:8000`); it is also the build loop. Chrome or Chromium is disclosed as the supported browser; others run labelled "capture chain unverified". A page opened from `file://` cannot load the Python runtime (Chrome blocks `fetch()` there), so the page checks its protocol and shows "open the hosted link or serve the folder" instead of a silently dead analysis.

## 3. Analysis route: Pyodide, the exact cores

The cores run in-browser unmodified, vendored the Intake Scanner way: `vendor_cores.sh`, `VENDORED.json` (repo, commit, sha256 per file), a start check that refuses on mismatch, copies never edited here. Vendored: `level_check_phrase_core.py`; `noise_profile_scanner_core.py` with `platform_band.yaml`; `rt60_analyzer_core_v5.py` with `platform_band`, `strict`, `forgiving` yamls; from the platform, `provenance.py` (`verify_samples`, `ceiling_from_provenance`) and a `ceiling.py` lifted from `_env_ceiling`, `_device_ceiling` and the `min()` in `audition_reviews.py` plus `AUDIO_TIERS` in `tiers.py`.

Dependencies checked today against Pyodide 314.0.7: numpy, scipy, pyyaml, soundfile and soxr are built in; pyloudnorm is not, so its pure-Python wheel (plus `future`) ships in the repo and nothing is fetched at run time. The Pyodide subset is vendored too, 32 MB once (32,065,386 bytes as vendored at M1), so a fork is self-contained and works offline or on an intranet (D2). Parity: one harness runs `Screening Pipeline/Testing corpus` through the cores in the venv and in Pyodide's Node package and diffs the records field by field. A TypeScript port, if ever, comes later with Pyodide as oracle.

## 4. The pipeline, mirrored from the platform

1. **Capture:** mic with AGC, noise suppression and echo cancellation requested off, mono; float32 at the context rate (48 kHz, or 44.1 kHz when the track reports exactly 44100); `encodeWav24` into Pyodide's in-memory file system. Lifted from `app/static/index.html`: `getMicConstraints`, `checkMicProcessing`, the meter worklet, `encodeWav24`, `browserFingerprint`, `IS_MOBILE`.
2. **Format:** `verify_samples` on the native-rate samples with `lossless=True`; `confirmed_native` gives 4, else 3. A 44.1 kHz chain can never reach 4 (no energy above 22.05 kHz to find); the UI says so.
3. **Working copy:** 48 kHz, 24-bit, mono via soxr when not already 48 kHz, as the platform's ingest does.
4. **Level** (`scan_phrase`): true peak, integrated LUFS, A-weighted speech level, crest; peak PASS/HOT/CLIP, loudness PASS/QUIET/TOO_QUIET; FAIL is a stage gate.
5. **Quiet room** (`scan_noise_profile`, speech level handed over from 4, selected profile): floor dBA and dBFS, SNR; PASS/CAUTION/WARN/REJECT; writes `_np.wav` and a JSON sidecar.
6. **Room reflection** (`analyze_file` with that `_np.wav`, selected profile): RT60 at 1 kHz (T20), clap count, r²; pass/production/borderline/reject/error.
7. **Ceiling:** noise WARN or REJECT caps 2, CAUTION 3; RT60 borderline or reject caps 2, production 3; lower wins. Device: 2 when the browser self-reports mobile. Result `min(format, environment, device)`, named Reject / Lo-fi / Good / Hi-fi. On a gate failure (level FAIL, noise REJECT, RT60 reject or error) the platform assigns no tier: the summary says "no ceiling, fix this and re-run", numbers stay visible, the would-be cap is greyed (D3).

## 5. The bypass check

After `getUserMedia` with the three flags false, re-apply `autoGainControl:false` once, then read `track.getSettings()`. All false: **verified**. Any true: **processing active**, the stage does not run; the platform is warn-only here, but a noise profile taken through suppression is worthless and misleads the denoiser (D4). Field absent (Safari, WebKit, some Firefox): **unverified**, measurements run, the label stays on the result and the format ceiling is held at 3. Not visible to any readback: processing upstream of the browser such as macOS Voice Isolation; a hard-walled spectrum usually reads `indeterminate` in step 2, which already holds 3. Stated as a does-not-say note.

## 6. Profile selector

Default **Platform Band**, the ruled policy (noise reject below 20 dB SNR, WARN below 48, CAUTION below 55; RT60 0.25 / 0.55 / 0.90 s, `dry_room_pass`, r² at or above 0.85). Alternates: the cores' built-in defaults (noise reject below 38; RT60 Strict 0.25 / 0.50, r² 0.95), Strict, Forgiving, and a user-supplied yaml via a file picker that never uploads. Every result stamps `profile_name` and `config_sha256` per core, the platform's AB-6 stamp. The level check has no profile; its thresholds sit in the core's `DEFAULT_CONFIG`, stamped by that config's sha.

## 7. Noise-profile save

On the quiet-room result, "Save noise profile" uses the File System Access picker (Chrome; plain download as fallback) and writes two files locally: the trimmed `_np.wav` room tone, which is exactly what the denoiser's room-tone anchor consumes, and the core's JSON record (floor dBA and dBFS, SNR, speech level, disposition, thresholds and sha, durations, versions) extended with the capture readback (the three flags, track and context rates, browser fingerprint, device class). Gap: your note lists a spectrum and hum flags; the core's record has neither, and the denoiser runs its own hum detection on the WAV. Adding them is new code outside the cores (D5).

## 8. The phrase

Ships with the platform's en-US gate phrase ("The busy port keeps shipping fresh produce, but the price still surprises most customers.") as a measurement aid for the five-second level take. The core measures level, never content, so any read in any language works; the UI says "read this, or anything else, for five seconds", and the README carries replace-it instructions.

## 9. Output: per-metric status and margins

A summary card: the ceiling and its three components. Then one row per metric: value and unit, the band it landed in, and the margin to the nearest thresholds of the selected profile ("SNR 51.2 dB, CAUTION, 3.2 dB above WARN, 3.8 dB below PASS"), plus a status word: **measured** (true peak, LUFS, floor), **derived** (SNR, crest), **estimated** (RT60, with clap count and r²), **reported** (browser readback), **self-reported** (device class) (D6). This is the platform's `precise` disclosure with `safe_reason` not applied, on purpose: reasons unstripped, numbers exact, thresholds public by construction (keep consistent with the showcase disclosure pass).

## 10. The honest ceiling

Every number is a property of this capture chain, in this room, now, in uncalibrated dBFS. The tool says whether a take would clear the platform's gates and the highest tier it could earn there. It does not predict word error rate, training quality or a reviewer's score, and SNR is not a recognition predictor. Section 11 is documentation over the tiers, not a measurement.

## 11. Tier-to-fitness table (Rick writes)

| Ceiling | Platform name | TTS-grade | ASR-grade | Notes |
|---|---|---|---|---|
| 4 | Hi-fi | | | |
| 3 | Good | | | |
| 2 | Lo-fi | | | |
| none | Reject (a gate failed) | | | |

## 12. Repo and increments

This folder; remote `ricktorres25/recqc-standalone`; public from day one (Pages). Layout: `index.html`, `app/`, `py/` (cores, yamls, `VENDORED.json`, `vendor_cores.sh`, `recqc_runner.py` as the thin orchestration), `pyodide/`, `sw.js`, `manifest.webmanifest`, `tests/`, `README.md`, this note. Zero recurring cost; a custom domain is the only optional money. Weekend-sized increments, each ending at your gate: **M1** Pyodide loads the cores and reproduces the corpus records, no mic. **M2** capture, bypass check, level. **M3** quiet room, selector, profile save. **M4** clap, ceiling card, margins. **M5** PWA, Pages, README, parity in CI.

## Decisions for Rick

- **D2** Vendor the Pyodide subset (recommended) or load from a CDN.
- **D3** Gate failure: no ceiling, numbers shown, would-be cap greyed (recommended) or print a ceiling anyway.
- **D4** Processing active: refuse the stage (recommended) or warn and run like the platform.
- **D5** Noise JSON: core record plus readback now (recommended) or add spectrum and hum fields.
- **D6** The status vocabulary in section 9.
