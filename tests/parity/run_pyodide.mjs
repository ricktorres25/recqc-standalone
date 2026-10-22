// run_pyodide.mjs - the Pyodide side of the parity test.
//
// Runs RecQC's runner over prepared folders of takes in Pyodide under Node and writes
// the records as JSON. It loads the vendored pyodide/ folder directly (no npm install)
// through the same boot module the browser uses. The work folder is mounted into
// Pyodide with NODEFS, so the cores read the WAVs from disk and write their outputs
// (_np.wav, take logs) there.
//
// packageBaseUrl points at the vendored folder, so a missing wheel fails instead of
// being downloaded from the CDN into pyodide/ (parity.py also checks pyodide/ is
// unchanged afterwards).
//
// Python builds the whole output and Node writes it through unparsed: a record may
// hold NaN, which Python's json writes but JavaScript's JSON.parse rejects.
//
// Usage:  node tests/parity/run_pyodide.mjs <work dir> <out.json> <set> [<set> ...]
//         where a set is <name>:<profile> (or just <profile>, named after it), scored
//         by run_corpus, or page:<name>:<noise profile>, scored by run_page_corpus (the
//         page's flow), and <work dir>/<name>/ holds one folder of takes per corpus folder.
import { readFile, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { loadPyodide } from "../../pyodide/pyodide.mjs";
import { bootRecQC } from "../../app/recqc_boot.mjs";

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const [workDir, outPath, ...sets] = process.argv.slice(2);
if (!workDir || !outPath || sets.length === 0) {
  console.error("usage: node tests/parity/run_pyodide.mjs <work dir> <out.json> <set> [...]");
  process.exit(2);
}

const indexURL = join(REPO, "pyodide") + "/";   // in Node this must be a path, not a URL
const t0 = performance.now();
const pyodide = await bootRecQC({
  loadPyodide,
  indexURL,
  readBytes: async (rel) => new Uint8Array(await readFile(join(REPO, rel))),
  pyodideOptions: { packageBaseUrl: indexURL },
  log: (m) => console.error(`[pyodide] ${m}`),
});
const bootSeconds = ((performance.now() - t0) / 1000).toFixed(2);

pyodide.FS.mkdirTree("/work");
pyodide.FS.mount(pyodide.FS.filesystems.NODEFS, { root: resolve(workDir) }, "/work");

// JSON string and list literals are valid Python literals for these values.
const json = pyodide.runPython(`
import json, sys
from importlib.metadata import version
import numpy, scipy, soundfile, soxr, yaml
import recqc_runner

result = {
    "pyodide_version": ${JSON.stringify(pyodide.version)},
    "node_version": ${JSON.stringify(process.version)},
    "boot_seconds": ${bootSeconds},
    "libraries": {"python": sys.version.split()[0], "numpy": numpy.__version__,
                  "scipy": scipy.__version__, "soundfile": soundfile.__version__,
                  "soxr": soxr.__version__, "PyYAML": yaml.__version__,
                  "pyloudnorm": version("pyloudnorm"), "future": version("future")},
    "profiles": {},
    "pages": {},
}
for spec in ${JSON.stringify(sets)}:
    if spec.startswith("page:"):
        name, _, profile = spec[len("page:"):].partition(":")
        result["pages"][name] = recqc_runner.run_page_corpus(f"/work/{name}", profile)
        print(f"[pyodide] {name}: {len(result['pages'][name])} folders, page flow", file=sys.stderr)
        continue
    name, _, profile = spec.partition(":")
    result["profiles"][name] = recqc_runner.run_corpus(f"/work/{name}", profile or name)
    print(f"[pyodide] {name}: {len(result['profiles'][name])} folders", file=sys.stderr)
json.dumps(result)
`);
await writeFile(outPath, json);
