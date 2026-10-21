// recqc_boot.mjs - start Pyodide and load RecQC's Python side, in the browser or in Node.
//
// The same steps in both places, so the parity test exercises the browser's path:
//   1. start Pyodide from the vendored pyodide/ folder (no CDN)
//   2. load the wheels RecQC needs from that folder
//   3. copy py/ (the vendored cores, their manifest, the runner) into Pyodide's
//      in-memory file system under /recqc/py
//   4. check the pyloudnorm wheel against pyodide/VENDORED.json, then install it
//      (it is pure Python and not part of Pyodide)
//   5. import the runner; importing the cores package runs the start check first
//
// The caller passes loadPyodide (imported from pyodide/pyodide.mjs), the indexURL of
// the vendored folder, and readBytes(relPath), which returns a file of this repo as a
// Uint8Array: fetch() in the browser, fs.readFile in Node.

export const PY_ROOT = "/recqc/py";

// Pyodide's own packages RecQC uses; the lock adds their dependencies (cffi, pycparser).
const PACKAGES = ["numpy", "scipy", "pyyaml", "soundfile", "soxr", "future"];

// Runs inside Pyodide: refuse a wheel whose bytes differ from the manifest, else unpack
// it into site-packages (a pure-Python wheel needs nothing more to be importable).
const INSTALL_WHEEL = `
import hashlib, importlib, site, zipfile
found = hashlib.sha256(open(wheel_path, "rb").read()).hexdigest()
if found != expected_sha256:
    raise RuntimeError(f"{wheel_path} differs from pyodide/VENDORED.json (sha256 {found[:12]})")
zipfile.ZipFile(wheel_path).extractall(site.getsitepackages()[0])
importlib.invalidate_caches()
`;

export async function bootRecQC({ loadPyodide, indexURL, readBytes, pyodideOptions = {}, log = () => {} }) {
  const t0 = performance.now();
  const seconds = () => ((performance.now() - t0) / 1000).toFixed(1);
  const text = new TextDecoder();
  const readJSON = async (rel) => JSON.parse(text.decode(await readBytes(rel)));

  const pyodide = await loadPyodide({ indexURL, ...pyodideOptions });
  log(`Pyodide ${pyodide.version} started (${seconds()} s)`);
  await pyodide.loadPackage(PACKAGES, { messageCallback: () => {} });
  log(`packages loaded (${seconds()} s)`);

  // The vendored files are the ones the manifest names; the rest are RecQC's own.
  const manifest = await readJSON("py/cores/VENDORED.json");
  const pyFiles = ["cores/__init__.py", "cores/_vendor_check.py", "cores/VENDORED.json",
                   "recqc_runner.py", ...manifest.files.map((f) => `cores/${f.path}`)];
  for (const rel of pyFiles) {
    const dest = `${PY_ROOT}/${rel}`;
    pyodide.FS.mkdirTree(dest.slice(0, dest.lastIndexOf("/")));
    pyodide.FS.writeFile(dest, await readBytes(`py/${rel}`));
  }

  const wheel = (await readJSON("pyodide/VENDORED.json")).files
    .find((f) => f.path.startsWith("pyloudnorm-"));
  pyodide.FS.writeFile(`/tmp/${wheel.path}`, await readBytes(`pyodide/${wheel.path}`));
  // JSON string literals are valid Python string literals for these ASCII values.
  pyodide.runPython(`wheel_path = ${JSON.stringify(`/tmp/${wheel.path}`)}\n` +
                    `expected_sha256 = ${JSON.stringify(wheel.sha256)}\n` + INSTALL_WHEEL);

  pyodide.runPython(`import sys\nsys.path.insert(0, ${JSON.stringify(PY_ROOT)})\nimport recqc_runner`);
  log(`cores passed the start check; ready (${seconds()} s)`);
  return pyodide;
}
