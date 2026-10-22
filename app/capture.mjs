// capture.mjs - RecQC's mic capture: the platform's capture path, without its page.
//
// Everything that touches the audio is the platform's own code, lifted verbatim into
// platform_capture.mjs: the mic constraints (AGC, noise suppression and echo
// cancellation requested off), the mic-processing readback, the meter worklet and the
// 24-bit WAV encoder. This file is the glue the platform's page wraps around them
// (MAD/Talent Platform app/static/index.html: startMeter, startRecording, stopRecording,
// assembleCapture), without that page's UI and its meter-recovery machinery.
import { WORKLET_CODE, encodeWav24, getMicConstraints, checkMicProcessing,
         IS_MOBILE, shared } from "./platform_capture.mjs";

// The platform's values: 200 ms recorded before a take shows as recording and after
// Stop, and each stage's auto-stop (its STAGE_INFO), counted from the start of the
// pre-roll.
const PRE_ROLL_MS = 200, POST_ROLL_MS = 200;
export const MAX_SECONDS = { phrase: 20, noise: 12, clap: 30 };

// The platform's clap auto-stop (countClapIfImpulse, clapAutoStopDue and their
// constants): a meter window that peaks at or above the RT60 core's clap-detection
// floor counts as a clap, at most once per the core's minimum clap spacing, so the page
// counts the events the core will detect; the take stops once the tail after the
// target clap has run, so the last decay is never cut off. It conditions on the count
// of claps only, never on the measurement: a short take is completed by asking for
// more claps, not by stopping early.
export const CLAP_COUNT_FLOOR_DB = -20, CLAP_COUNT_REFRACTORY_SEC = 1.5, CLAP_TARGET = 6, CLAP_TAIL_SEC = 3;
// The platform meter's clap range, shaded while claps record: its floor is that
// detection floor, its top the meter's -3 dBFS warn line.
export const CLAP_RANGE_DB = [-20, -3];

export class ClapCounter {
  constructor() { this.reset(false); }
  reset(on) { this.counting = on; this.count = 0; this.lastAt = 0; this.targetAt = 0; }
  // One meter message: its window's peak, and when it arrived, in seconds.
  onPeak(peakDb, now) {
    if (!this.counting || !Number.isFinite(peakDb)) return;
    if (peakDb >= CLAP_COUNT_FLOOR_DB && now - this.lastAt >= CLAP_COUNT_REFRACTORY_SEC) {
      this.lastAt = now;
      this.count++;
      if (this.count >= CLAP_TARGET && !this.targetAt) this.targetAt = now;
    }
  }
  stopDue(now) { return this.targetAt > 0 && now - this.targetAt >= CLAP_TAIL_SEC; }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// The mic-processing check (design note section 5): all three flags read false is
// "verified"; any reads true is "active" (the level stage does not run, D4); any not
// reported, as on Safari, WebKit and some Firefox, is "unverified" (the stage runs and
// the format ceiling is held at 3).
export function bypassState(readback) {
  const flags = [readback.agc, readback.ns, readback.ec];
  if (flags.some((v) => v === true)) return "active";
  if (flags.every((v) => v === false)) return "verified";
  return "unverified";
}

export class Capture {
  constructor(onMeter = () => {}) {
    this.onMeter = onMeter;   // gets each meter message from the worklet, about every 20 ms
    this.stream = null;
    this.ctx = null;
    this.node = null;
    this.chunks = [];
    this.bookkeeping = null;
    this.ended = null;
  }

  // Open the mic and start the meter. Returns the mic readback.
  async arm() {
    this.stream = await navigator.mediaDevices.getUserMedia(getMicConstraints());
    // The capture rate, picked as the platform's startMeter picks it: only a device that
    // reports exactly 44100 Hz records at 44.1 kHz; anything else, including a browser
    // that reports no rate, at 48 kHz. The context's actual rate is what counts after.
    let rate = 48000;
    try {
      const track = this.stream.getAudioTracks()[0];
      if (track && track.getSettings && track.getSettings().sampleRate === 44100) rate = 44100;
    } catch (e) { /* keep 48 kHz */ }
    try { this.ctx = new AudioContext({ sampleRate: rate, latencyHint: "interactive" }); }
    catch (e) { this.ctx = new AudioContext({ latencyHint: "interactive" }); }

    // The platform's page reads the processing flags before its meter context exists;
    // RecQC reads them after, so the readback carries the context rate as well.
    shared.audioCtx = this.ctx;
    await checkMicProcessing(this.stream);

    const url = URL.createObjectURL(new Blob([WORKLET_CODE], { type: "application/javascript" }));
    await this.ctx.audioWorklet.addModule(url);
    URL.revokeObjectURL(url);
    this.node = new AudioWorkletNode(this.ctx, "meter-processor",
      { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1] });
    this.node.port.onmessage = ({ data }) => this.fromWorklet(data);
    // As on the platform, the worklet feeds the speakers through a gain of 0: the graph
    // keeps running and nothing is heard.
    const source = this.ctx.createMediaStreamSource(this.stream);
    const mute = this.ctx.createGain();
    mute.gain.value = 0;
    source.connect(this.node);
    this.node.connect(mute);
    mute.connect(this.ctx.destination);
    await this.ctx.resume();
    return this.readback();
  }

  fromWorklet(d) {
    if (!d || !d.type) return;
    if (d.type === "meter") this.onMeter(d);
    else if (d.type === "capture-chunk") this.chunks.push(d.samples);
    else if (d.type === "capture-bookkeeping") this.bookkeeping = d.report;
    else if (d.type === "capture-end" && this.ended) { this.ended(); this.ended = null; }
  }

  // The mic-processing flags (undefined when the browser does not report one), both
  // rates, the browser fingerprint and the self-reported device class.
  readback() {
    const p = shared.procStatus || {};
    return { agc: p.agc, ns: p.ns, ec: p.ec,
             track_sample_rate: p.track_sample_rate ?? null,
             context_sample_rate: p.context_sample_rate ?? null,
             browser: shared.lastBrowserFingerprint, is_mobile: IS_MOBILE };
  }

  // Start a take: the meter's totals reset, and the pre-roll records before this returns.
  async start() {
    this.chunks = [];
    this.bookkeeping = null;
    this.node.port.postMessage({ type: "reset" });
    this.node.port.postMessage({ type: "capture-start" });
    await sleep(PRE_ROLL_MS);
  }

  // End the take after the post-roll. Returns {samples, rate, wav, bookkeeping}, where
  // wav is the platform's 24-bit mono WAV of the samples (null when nothing came in).
  async stop() {
    await sleep(POST_ROLL_MS);
    const ended = new Promise((resolve) => { this.ended = resolve; });
    this.node.port.postMessage({ type: "capture-stop" });
    await Promise.race([ended, sleep(3000)]);   // the platform waits at most 3 s too
    // As the platform's assembleCapture: the chunks, end to end.
    const samples = new Float32Array(this.chunks.reduce((n, c) => n + c.length, 0));
    let at = 0;
    for (const c of this.chunks) { samples.set(c, at); at += c.length; }
    this.chunks = [];
    const rate = this.ctx.sampleRate;
    return { samples, rate, bookkeeping: this.bookkeeping,
             wav: samples.length ? encodeWav24(samples, rate) : null };
  }

  // Close the mic and the meter.
  disarm() {
    if (this.stream) this.stream.getTracks().forEach((t) => t.stop());
    if (this.ctx) this.ctx.close().catch(() => {});
    this.stream = null;
    this.ctx = null;
    this.node = null;
  }
}
