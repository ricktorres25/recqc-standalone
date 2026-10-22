// recqc_worker.mjs - RecQC's analysis thread: Pyodide and the vendored cores, off the page.
//
// A module worker (Pyodide 314 will not start in a classic one), so the page and its
// meter stay live while Python boots and scores. It boots through the same bootRecQC
// as the M1 smoke test and the parity test, as soon as the page creates it.
//
//   page -> worker   {id, op: "level", wav: ArrayBuffer, bypass: "verified" | "unverified"}
//   worker -> page   {type: "log", message} while booting, then {type: "ready", version}
//                    or {type: "boot-failed", error}; per request {id, ok: true, result}
//                    or {id, ok: false, error}
import { loadPyodide } from "../pyodide/pyodide.mjs";
import { bootRecQC } from "./recqc_boot.mjs";

const base = new URL("../", import.meta.url);   // the repo root, as served
const readBytes = async (rel) => {
  const r = await fetch(new URL(rel, base));
  if (!r.ok) throw new Error(`${rel}: HTTP ${r.status}`);
  return new Uint8Array(await r.arrayBuffer());
};

// Python returns JSON text; NaN and Infinity are not valid JSON, so they become null.
const parse = (text) => JSON.parse(text.replace(/-?\bInfinity\b|\bNaN\b/g, "null"));

const ready = bootRecQC({
  loadPyodide, indexURL: new URL("pyodide/", base).href, readBytes,
  log: (message) => postMessage({ type: "log", message }),
}).then(
  (pyodide) => { postMessage({ type: "ready", version: pyodide.version }); return pyodide; },
  (e) => { postMessage({ type: "boot-failed", error: String(e.message ?? e) }); throw e; });
ready.catch(() => {});   // reported above; each request below reports it again

let takes = 0;
onmessage = async ({ data }) => {
  const { id, op } = data;
  try {
    const pyodide = await ready;
    if (op !== "level") throw new Error(`unknown request: ${op}`);
    // Each take gets its own folder, removed afterwards, so takes never meet in memory.
    const dir = `/takes/${++takes}`;
    pyodide.FS.mkdirTree(dir);
    pyodide.FS.writeFile(`${dir}/recqc_phrase.wav`, new Uint8Array(data.wav));
    // JSON string literals are valid Python string literals for these ASCII values.
    const text = pyodide.runPython(`
import json, shutil, recqc_runner
try:
    out = json.dumps(recqc_runner.run_level_take(${JSON.stringify(`${dir}/recqc_phrase.wav`)}, ${JSON.stringify(data.bypass)}))
finally:
    shutil.rmtree(${JSON.stringify(dir)}, ignore_errors=True)
out
`);
    postMessage({ id, ok: true, result: parse(text) });
  } catch (e) {
    postMessage({ id, ok: false, error: String(e.message ?? e).split("\n").slice(-3).join("\n") });
  }
};
