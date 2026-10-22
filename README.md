# RecQC Standalone

A recording-chain and room check that runs in your browser, on your machine. It is the audio side of the MAD collection platform's Rec QC: a level check, a quiet-room measurement and a clap-based room-reflection measurement, scored under the platform's published policy. It prints the audio tier ceiling a take could earn, with every measurement, threshold and margin shown. Nothing is uploaded.

**Status:** complete through M5. The whole check runs in the browser, scored by the platform's own cores. It installs as an app that works with the network off, and every push is checked by CI. [DESIGN_NOTE.md](DESIGN_NOTE.md) is the approved design.

## What it does

- Runs the platform's exact Python measurement cores in the browser (Pyodide), vendored with their source commit and hash.
- Checks that the browser's microphone processing (gain control, noise suppression, echo cancellation) is really off before measuring.
- Checks every take for audio frames dropped in the browser's capture path, with the platform's own capture-continuity check (warn-only, as on the platform).
- Measures the room's reverberation time (RT60) from claps, collecting claps the way the platform does.
- Prints the audio tier ceiling, the lowest of its format, environment and device caps, with each measurement's distance from the nearest threshold.
- Scores each room check under a selectable profile (the platform's Platform Band by default, or your own yaml) and stamps every result with the profile's hash.
- Saves the measured noise profile to disk as a room-tone WAV plus a JSON record.
- Runs hosted (GitHub Pages) or installed (works offline); forks and intranets serve the folder with any static server.

## Quick start

Open <https://ricktorres25.github.io/recqc-standalone/> in Chrome, or serve this folder yourself:

    python3 -m http.server 8000

and open <http://localhost:8000> in Chrome. A page opened straight from the file (`file://`) cannot load the Python runtime, and says so.

1. Wait for "Analysis engine ready". The first visit downloads about 32 MB (Python and the cores).
2. Turn on the mic. Chrome asks for permission the first time.
3. Press Record, read the phrase for about five seconds, and press Stop. The phrase is a text box: write your own, in any language, and this browser remembers it.
4. Read the result: the mic-processing check, the format verdict and the level check, each with its numbers and reasons.
5. Once the level check passes, press Record room tone and stay silent and still for at least five seconds (it stops on its own at 12 s). Leave your gain where it was: the signal-to-noise ratio is the level take's speech level over this room's noise floor.
6. On a quiet room that passes, Save noise profile writes the room tone, trimmed to its longest unbroken quiet stretch, as a 48 kHz, 24-bit WAV, with the measurement record as JSON. Chrome asks for a folder; other browsers download both files.
7. Once the quiet room passes, press Record claps, take three steps back from the mic and clap with cupped hands each time CLAP NOW shows; it pauses while the meter's peak-hold line holds your last clap. The take stops on its own, three seconds after the sixth clap it counts (or at 30 s). If the claps did not give a clear enough read, Add more claps adds a take to them and the whole set is scored again, as on the platform. In a room whose quiet room reads WARN this check does not run: the platform skips it there, because the noise alone caps the tier at 2.
8. Read the result at the top: the tier ceiling, the three caps that set it and why, and under each take every measurement on its band rail, with how far it sits from the nearest threshold. If a check failed its gate there is no ceiling, as on the platform; the page says which check, and what the ceiling would have been.

The live meter and the mic check stay pinned on the left (on a phone, across the top) while you work down the page.

Every take also carries the platform's capture-continuity check. While recording, the meter counts the audio frames the browser hands it; if any went missing, or the count does not add up, the take reads "suspect" and shows the platform's warning to play it back and re-record if you hear a click or a skip. As on the platform it is a warning only: it does not gate a check or move the ceiling. On a phone the mic check also shows the platform's notice that phone captures can drop audio under load.

Each room check has its own "Scored under" selector: the Default band, which is what the live platform runs (its Platform Band profile, the name its records carry), or your own yaml (see [Profiles](#profiles)). Changing a profile re-scores the takes you already have; a new quiet-room profile re-scores the claps too, since they read its room tone.

Every take can be played back from its waveform, and a click on the waveform moves the playhead there. Playback stops when a take starts recording. The room tone, near silent as recorded, can also be played raised to a -6 dBFS peak.

Add `?debug` to the address for a "Download this take" link.

## Run modes

**Hosted.** <https://ricktorres25.github.io/recqc-standalone/>, served by GitHub Pages from this repository. Fork the repository and turn on Pages (deploy from the branch, root folder) to host your own copy.

**Installed.** Once the hosted page has loaded, Chrome offers to install it: the install icon at the right of the address bar, or ⋮ → Cast, save and share → Install page as app. The installed app opens in its own window from the Dock (or the Start menu), and runs with the network off: the page, the Python runtime, the cores and the fonts are kept in the browser (about 32 MB), each file checked against its hash when it is stored. A new version is fetched in the background and takes over the next time the app is opened, so nothing changes in the middle of a check. To remove it, ⋮ → Uninstall in the app's window.

**Self-hosted.** Serve the folder with any static server, as in the quick start. Chrome allows the microphone only on `https://` addresses and on `localhost`, so an intranet copy needs HTTPS; a plain `http://` address on your network loads the page but cannot open the mic. On `localhost` the offline copy is off, so a change to the files shows on the next reload; add `?sw` to the address to turn it on there (for testing the installed app before you publish).

**Forks that change a file.** After changing any file the page loads, run `python3 py/build_precache.py`, which rewrites the offline copy's file list in `sw.js` with each file's hash. Without it the installed app keeps the old version (and CI fails with "STALE").

## Profiles

Each room check is scored under one profile. The page offers the **Default band**, which is the live platform's policy (its records name it Platform Band), or your own yaml, picked from your disk and read on your machine, never uploaded:

| Check | Bands under the Default band |
|---|---|
| Quiet room (A-weighted SNR) | REJECT below 20 dB, WARN below 48, CAUTION below 55, PASS from 55 |
| Room reflection (RT60 at 1 kHz) | pass up to 0.25 s, production up to 0.55, borderline up to 0.90, reject above; at least 3 usable claps, each fit with r² at or above 0.85 |

Two other profiles stay in the runner for the command line and the tests, not on the page: the quiet room's built-in **Default** (REJECT below 38 dB) and room reflection's **Strict** (0.25 / 0.50 s, r² 0.95, also its built-in) and **Forgiving** (0.42 / 0.80 s, r² 0.95).

### Writing your own

A profile is a yaml file that must set `profile_name` and carry the check's own section, `snr` for the quiet room or `gate` for room reflection. One without them is refused, so no result is ever stamped with a name that is not its own. The file is merged over the core's **built-in** config, which is the quiet room's Default (REJECT below 38 dB) or room reflection's Strict, not over the Default band. So a key you leave out takes the built-in's value; the templates below state every policy value, so an edit starts from the platform's policy and moves only what you change.

Every result made under your profile carries its `profile_name` and a `config_sha256` of the full config the core ran, so two results can be checked for having run the same policy.

**Quiet room** (save as, say, `my_quiet_room.yaml`):

```yaml
# A RecQC quiet-room profile. Copy, rename and change the values you need.
profile_name: "My quiet room"     # required: names every result made under this file

snr:                              # required: A-weighted SNR = speech level - noise floor, in dB
  reject_below: 20.0              # below this, REJECT: the stage gate fails and there is no ceiling
  warn_below: 48.0                # below this, WARN: caps the tier at 2, and room reflection is skipped
  caution_below: 55.0             # below this, CAUTION: caps the tier at 3
  pass_above: 55.0                # from this up, PASS: no cap from the noise

# The keys below change what counts as a valid recording, not the policy. The shipped
# profiles never set them; leave them out unless you mean to change the measurement.
# recording:
#   min_duration_sec: 3.0         # a shorter room-tone take is rejected
#   min_valid_noise_sec: 3.0      # the shortest unbroken quiet stretch the floor may be measured on
```

**Room reflection** (save as, say, `my_room_reflection.yaml`):

```yaml
# A RecQC room-reflection profile. Copy, rename and change the values you need.
profile_name: "My room reflection" # required: names every result made under this file

gate:                             # required: the RT60 bands, in seconds, at 1 kHz
  pass_below: 0.25                # up to this, pass: no cap from the room
  production_below: 0.55          # up to this, production: caps the tier at 3 (leave out for three bands)
  reject_above: 0.90              # above this, reject: the stage gate fails; below it, borderline caps at 2
  min_valid_claps: 3              # fewer usable claps and the room is unmeasured
  dry_room_pass: true             # a room too dry to measure passes when its claps agree on it

quality:
  fit_window: "T20"               # the decay range each clap's RT60 is fitted over
  r_squared_min: 0.85             # a clap whose fit is below this is not used

# The scanner's clap detection, for capture protocols other than cupped hands at 1 to
# 1.5 m. The shipped profiles never set these.
# advanced:
#   clap_peak_threshold_dbfs: -20.0
#   min_inter_clap_sec: 1.5
```

The noise core's full set of keys is `DEFAULT_CONFIG` in [py/cores/noise_profile_scanner_core.py](py/cores/noise_profile_scanner_core.py); the RT60 core's is `DEFAULT_CONFIG` in [py/cores/rt60_analyzer_core_v5.py](py/cores/rt60_analyzer_core_v5.py), and [py/cores/configs/rt60/platform_band.yaml](py/cores/configs/rt60/platform_band.yaml) records why each Platform Band value is what it is.

## The level-check phrase

The page ships with the platform's en-US gate phrase, "The busy port keeps shipping fresh produce, but the price still surprises most customers." It is a measurement aid: the level check measures level, never words, so any read in any language works, as long as it is about five seconds of your normal speaking voice. Rewrite the phrase in the text box and this browser remembers it (in its own storage, nowhere else); "Back to the default phrase" puts it back.

To ship a fork with another default phrase, change the text inside `<textarea id="phrase">` in `index.html`, then run `python3 py/build_precache.py`. A phrase that balances plosives (p, b, t, k) and sibilants (s, sh) shows the peaks a real session will make.

## Noise profile save

On a quiet room that passes, Save noise profile writes two files:

- `recqc_<date>_<time>_np.wav`: the room tone, trimmed to its longest unbroken quiet stretch (the stretch the floor was measured on), 48 kHz, 24-bit mono. This is the room-tone input a denoiser's noise profile takes.
- `recqc_<date>_<time>_np.json`: the noise core's record (floor in dBA and dBFS, SNR, speech level, disposition, the thresholds and their hash, durations, versions), plus what the browser reported about the capture: the three processing flags, the track and context sample rates, the browser fingerprint, the device class, and the room-tone take's capture-continuity report (verdict and frame counts).

Chrome asks for a folder and writes both there; other browsers download them. The record carries no spectrum and no hum flags; a denoiser that needs them measures them from the WAV.

## What the numbers mean, and do not mean

Each measurement shows its value, the band it landed in under the selected profile, and its margin to the nearest thresholds, for example "SNR 51.2 dB, CAUTION, 3.2 dB above WARN, 3.8 dB below PASS". A status word says what kind of number it is:

| Status | Meaning | Measurements |
|---|---|---|
| measured | read directly from the take | true peak, integrated loudness, noise floor |
| derived | computed from measured values | SNR, crest factor |
| estimated | fitted from a model of the take | RT60, with its clap count and r² |
| reported | what the browser says about itself | the mic-processing check |
| self-reported | what the device says it is | the device class |

Thresholds are public by construction: they are the profile's, shown on the page and stamped into every record.

The ceiling is the lowest of three caps. **Format:** 4 when the take is confirmed native at its rate, else 3; a 44.1 kHz chain can never reach 4 (it has no energy above 22.05 kHz to find), and a mic check that cannot be verified holds it at 3. **Environment:** a quiet room at WARN or REJECT caps at 2, CAUTION at 3; a room-reflection result of borderline or reject caps at 2, production at 3; the lower wins. **Device:** 2 when the browser reports a mobile device. If any gate fails (the level check, a quiet room at REJECT, room reflection at reject or error) there is no ceiling, as on the platform.

**The honest ceiling.** Every number is a property of this capture chain, in this room, now, in uncalibrated dBFS. The tool says whether a take would clear the platform's gates and the highest tier it could earn there. It does not predict word error rate, training quality or a reviewer's score, and SNR is not a recognition predictor. The table below is documentation over the tiers, not a measurement.

| Ceiling | For TTS | For ASR | What puts a take here |
|---|---|---|---|
| 4, Hi-fi | Usable with minimal or no pre-processing | Usable as recorded, though not ideal for a dataset meant to train for real-world, noisy conditions | No cap below 4: a format confirmed native (so a 48 kHz chain), a verified mic check, a quiet room at PASS, room reflection at pass, a desktop device |
| 3, Good | Usable once pre-processed to the lab's guidelines: typically EQ, compression, de-noise and de-reverb, and in some cases harmonic synthesis. A 44.1 kHz take is generally not preferred for TTS, even upsampled | Usable as recorded | The lowest cap is 3: a quiet room at CAUTION, a production room, or a format not confirmed native (every 44.1 kHz chain, and any take whose mic check is unverified) |
| 2, Lo-fi | Not recommended | Usable as recorded | The lowest cap is 2: a quiet room at WARN, a borderline room, or a mobile device |
| none, Reject | Not usable | Not usable | A gate failed (the level check, a quiet room at REJECT, room reflection at reject or error), and the platform assigns no tier. Fix that check and re-run |

## Vendored cores

The measurement and policy code is not reimplemented here; it is the platform's own, copied in by `py/vendor_cores.sh` from the MAD source repositories at a recorded commit:

- the level check, quiet-room and RT60 cores and their profile yamls, and the screening pipeline's stage gates, copied whole;
- the platform's provenance check (`verify_samples`) and tier names, copied whole;
- the environment and device caps, the working-copy rule, clap collection and the lo-fi band rule, lifted function by function from the platform's code, verbatim;
- the capture code (the mic constraints, the processing check, the meter worklet, the WAV encoder, and the capture-continuity check with its two notices), lifted from the platform's page into `app/platform_capture.mjs`.

`py/cores/VENDORED.json` and `app/VENDORED.json` record each file's source, commit and sha256. The Python side refuses to start if any vendored file differs from its hash, so the page never scores with an edited copy. The Pyodide runtime and its wheels (Pyodide 314.0.7, plus pyloudnorm 0.1.1 and the `future` package it needs, which Pyodide does not carry) are vendored the same way by `py/vendor_pyodide.sh` into `pyodide/`, so the page loads nothing from any CDN.

## Tests

| Test | What it checks |
|---|---|
| `tests/test_ceiling.py` | the ceiling, the caps and the margins, on hand-built results |
| `tests/test_profiles.py` | the profiles, your-yaml refusals, the quiet room and room reflection on synthetic takes, and the templates in this README |
| `tests/test_vendored.py` | every vendored file against its hash, and (where the source repositories are present) against its source at the recorded commit |
| `tests/test_offline.py` | the offline copy's file list is current and holds every file the page loads, and the install manifest |
| `tests/parity/parity.py` | the cores score each take the same in Pyodide (under Node) as in CPython, field by field, at 48 and 44.1 kHz, under each profile and through the page's flow, with near misses that must be caught |
| `tests/headless_capture.py` | the page in headless Chrome with a fake mic; `--offline` installs the offline copy, takes the server away and checks the page still loads and scores |
| `tests/verdict_length.py` | whether a 5 s take gets the same format verdict as a longer one |

Each test prints PASS, FAIL or NOT RUN per check and exits 0, 1 or 2; NOT RUN is never a pass. The parity test needs the live platform's library versions (`pip install -r tests/requirements.txt`, on Python 3.12) and Node.

**CI** ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs on every push: the offline copy's list and manifest, the vendored hashes, the ceiling and profile tests, parity on a synthetic corpus (`tests/parity/synth_corpus.py`, seven generated rooms that reach every branch of the page's flow), and the page in headless Chrome online and from the offline copy. What CI cannot run, and is checked on the maintainer's machine before a release: the vendored files against their source repositories, parity on the real recordings of the Testing corpus, including against its stored records, and the page with a recorded voice as the fake mic. The real recordings are not in this repository.

## Known limitations

- **Chrome or Chromium only is verified.** Other browsers run, with a note that the capture chain is unverified. Safari and some Firefox versions do not report whether their mic processing is off, so the mic check reads unverified and the format ceiling is held at 3.
- **The continuity check sees only the browser's own audio path.** A dropout below the browser (an audio interface, a phone or the operating system splicing audio at the device level) arrives with its frames contiguous and reads clean. That is why phones also get a standing notice, as on the platform.
- **Processing before the browser is invisible.** macOS Voice Isolation, an interface's onboard DSP or a virtual audio device can process the signal before Chrome sees it, and no readback shows it. A hard-limited spectrum usually reads indeterminate in the format check, which already holds the ceiling at 3.
- **A 5 s take can read a lower format verdict than a long one.** On a built-in laptop mic, the first 5 s of a 20 s read came out `confirmed_resampled` while the whole take came out native. Check your own chain with `tests/verdict_length.py` and a long take from `?debug`.
- **The automated page test runs at 44.1 kHz only.** Chrome's fake mic runs at 44.1 kHz, so `tests/headless_capture.py` covers that path; the 48 kHz path was checked by hand.
- **Uncalibrated levels.** dBFS depends on your gain; the SNR does not, as long as the gain does not move between the level take and the room tone.
- **The microphone needs HTTPS or localhost** (see [Run modes](#run-modes)).
- **The offline copy is about 32 MB** in each browser profile that installs or opens the hosted page.

## License

MIT. See [LICENSE](LICENSE).
