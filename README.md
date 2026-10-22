# RecQC Standalone

A recording-chain and room check that runs in your browser, on your machine. It is the audio side of the MAD collection platform's Rec QC: a level check, a quiet-room measurement and a clap-based room-reflection measurement, scored under the platform's published policy. It prints the audio tier ceiling a take could earn, with every measurement, threshold and margin shown. Nothing is uploaded.

**Status:** in development. The whole check runs: capture, the mic-processing check, the format verdict, the level, the room's noise floor and signal-to-noise ratio, the room's reverberation time from claps, and the tier ceiling with every margin, each room check under a selectable profile, scored in the browser by the platform's cores, with the room tone saveable as a noise profile. Installing it as an app for offline use comes next. [DESIGN_NOTE.md](DESIGN_NOTE.md) is the approved design.

## What it will do

- Run the platform's exact Python measurement cores in the browser (Pyodide), vendored with their source commit and hash.
- Check that the browser's microphone processing (gain control, noise suppression, echo cancellation) is really off before measuring.
- Measure the room's reverberation time (RT60) from claps, collecting claps the way the platform does.
- Print the audio tier ceiling, the lowest of its format, environment and device caps, with each measurement's distance from the nearest threshold.
- Score against a selectable policy profile (Platform Band by default) and stamp every result with the profile's hash.
- Save the measured noise profile to disk as a room-tone WAV plus a JSON record.
- Run hosted (GitHub Pages) or installed (PWA, works offline); forks and intranets serve the folder with any static server.

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

Each room check has its own "Scored under" selector: the Default band, which is what the live platform runs (its Platform Band profile, the name its records carry), or your own yaml. A quiet-room yaml sets `profile_name` and an `snr` section; a room-reflection yaml sets `profile_name` and a `gate` section. A yaml is read on your machine, never uploaded. Changing a profile re-scores the takes you already have; a new quiet-room profile re-scores the claps too, since they read its room tone.

Every take can be played back from its waveform, and a click on the waveform moves the playhead there. Playback stops when a take starts recording. The room tone, near silent as recorded, can also be played raised to a -6 dBFS peak.

Add `?debug` to the address for a "Download this take" link.

## Run modes

To be written. See the design note, section 2.

## Profiles

To be written (M5): how to write your own quiet-room and room-reflection profiles, with a template yaml for each that can be copied and edited (the keys each core reads, which ones `profile_name` and the `snr` or `gate` section must set, and what a value moves). See the design note, section 6.

## The level-check phrase

To be written, with instructions for replacing the default phrase in another language. See the design note, section 8.

## Noise profile save

To be written. See the design note, section 7.

## What the numbers mean, and do not mean

To be written. See the design note, sections 9 to 11.

## Vendored cores

To be written. The pattern follows the MAD Intake Scanner: `vendor_cores.sh`, `VENDORED.json`, a start check.

## Tests

To be written. A parity harness compares the cores' records in CPython and in Pyodide over a fixed take set.

## Known limitations

To be written.

## License

MIT. See [LICENSE](LICENSE).
