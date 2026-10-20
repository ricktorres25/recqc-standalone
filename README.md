# RecQC Standalone

A recording-chain and room check that runs in your browser, on your machine. It is the audio side of the MAD collection platform's Rec QC: a level check, a quiet-room measurement and a clap-based room-reflection measurement, scored under the platform's published policy. It prints the audio tier ceiling a take could earn, with every measurement, threshold and margin shown. Nothing is uploaded.

**Status:** design stage. [DESIGN_NOTE.md](DESIGN_NOTE.md) is under review; there is no code yet.

## What it will do

- Run the platform's exact Python measurement cores in the browser (Pyodide), vendored with their source commit and hash.
- Check that the browser's microphone processing (gain control, noise suppression, echo cancellation) is really off before measuring.
- Score against a selectable policy profile (Platform Band by default) and stamp every result with the profile's hash.
- Save the measured noise profile to disk as a room-tone WAV plus a JSON record.
- Run hosted (GitHub Pages) or installed (PWA, works offline); forks and intranets serve the folder with any static server.

## Quick start

To be written at M2 (first runnable milestone).

## Run modes

To be written. See the design note, section 2.

## Profiles

To be written. See the design note, section 6.

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
