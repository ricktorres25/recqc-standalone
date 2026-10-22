// recqc_worker.mjs - RecQC's analysis thread: Pyodide and the vendored cores, off the page.
//
// A module worker (Pyodide 314 will not start in a classic one), so the page and its
// meter stay live while Python boots and scores. It boots through the same bootRecQC
// as the M1 smoke test and the parity test, as soon as the page creates it.
//
//   page -> worker   {id, op: "level", wav: ArrayBuffer, bypass: "verified" | "unverified"}
//                    {id, op: "noise", wav: ArrayBuffer, speech_level_dba, profile}
//                    {id, op: "profile", core: "noise", profile}
//                    where profile is {name} for a shipped profile, or {name, text} for
//                    the user's yaml (its file name and contents)
//   worker -> page   {type: "log", message} while booting, then {type: "ready", version}
//                    or {type: "boot-failed", error}; per request {id, ok: true, result}
//                    or {id, ok: false, error}. A noise reply also carries np: the WAV
//                    bytes of the trimmed room tone, or null when the core wrote none.
//
// The worker keeps nothing between requests: the page holds the takes, and sends one
// again to re-score it under another profile.
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
// JSON string and number literals are valid Python literals for the values passed this
// way: folder paths, profile names and the speech level. The user's yaml and its file
// name never go into Python source; the yaml is written to a file instead.
const py = (value) => JSON.stringify(value);

const ready = bootRecQC({
  loadPyodide, indexURL: new URL("pyodide/", base).href, readBytes,
  log: (message) => postMessage({ type: "log", message }),
}).then(
  (pyodide) => { postMessage({ type: "ready", version: pyodide.version }); return pyodide; },
  (e) => { postMessage({ type: "boot-failed", error: String(e.message ?? e) }); throw e; });
ready.catch(() => {});   // reported above; each request below reports it again

// The runner's profile argument: a shipped name, or the path the user's yaml is written to.
function profileArg(pyodide, dir, profile) {
  if (profile.text == null) return profile.name;
  const path = `${dir}/your_profile.yaml`;
  pyodide.FS.writeFile(path, new TextEncoder().encode(profile.text));
  return path;
}

// One function per request: each gets the folder it may use and returns
// {result, np?, transfer?}.
const OPS = {
  level(pyodide, dir, { wav, bypass }) {
    const take = `${dir}/recqc_phrase.wav`;
    pyodide.FS.writeFile(take, new Uint8Array(wav));
    return { result: parse(pyodide.runPython(
      `import json, recqc_runner\njson.dumps(recqc_runner.run_level_take(${py(take)}, ${py(bypass)}))`)) };
  },

  noise(pyodide, dir, { wav, speech_level_dba, profile }) {
    if (!Number.isFinite(speech_level_dba)) throw new Error("no speech level from the level take");
    const take = `${dir}/recqc_noise.wav`;
    pyodide.FS.writeFile(take, new Uint8Array(wav));
    const result = parse(pyodide.runPython(
      `import json, recqc_runner\njson.dumps(recqc_runner.run_noise_take(${py(take)}, ` +
      `${py(speech_level_dba)}, ${py(profileArg(pyodide, dir, profile))}))`));
    // The trimmed room tone goes back to the page, which keeps it for "Save noise profile".
    let np = null;
    if (result.np) {
      const bytes = pyodide.FS.readFile(result.np.file);
      np = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
    }
    return { result, np, transfer: np ? [np] : [] };
  },

  profile(pyodide, dir, { core, profile }) {
    return { result: parse(pyodide.runPython(
      `import json, recqc_runner\njson.dumps(recqc_runner.describe_profile(${py(core)}, ` +
      `${py(profileArg(pyodide, dir, profile))}))`)) };
  },
};

let requests = 0;
onmessage = async ({ data }) => {
  const { id, op } = data;
  try {
    const pyodide = await ready;
    if (!Object.hasOwn(OPS, op)) throw new Error(`unknown request: ${op}`);
    // Each request gets its own folder, removed afterwards, so takes never meet in memory.
    const dir = `/takes/${++requests}`;
    pyodide.FS.mkdirTree(dir);
    try {
      const { result, np, transfer = [] } = OPS[op](pyodide, dir, data);
      postMessage(op === "noise" ? { id, ok: true, result, np } : { id, ok: true, result }, transfer);
    } finally {
      pyodide.runPython(`import shutil\nshutil.rmtree(${py(dir)}, ignore_errors=True)`);
    }
  } catch (e) {
    postMessage({ id, ok: false, error: String(e.message ?? e).split("\n").slice(-3).join("\n") });
  }
};
