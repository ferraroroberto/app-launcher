/* A dictation take captured as PCM while it records, as 16 kHz mono WAV.
 *
 * iPhone Safari records MediaRecorder output as fragmented MP4, and the PC then
 * needs ffmpeg to read it. This taps the same microphone stream with Web Audio
 * instead (an AudioWorklet, or a ScriptProcessor where the worklet cannot
 * load), downmixes to mono and resamples to 16 kHz as samples arrive, so the
 * take leaves the browser as WAV and no container is converted anywhere. The
 * samples go out one of two ways: as one WAV Blob when the take stops
 * (`stop()`), or, after `streamTo(sink)`, as one Blob a second (the first with
 * an open-ended WAV header, the rest bare samples) that joined in order are
 * one valid WAV. Every step is best effort: a null from here sends the caller
 * back to the recorder's own container.
 */

import { encodeWav, pcm16Bytes, wavHeader } from './voice-wav.js';

const OUT_RATE = 16000;
// Frames the worklet gathers before it posts them, so the main thread is not
// woken for every 128-frame render quantum.
const BATCH_FRAMES = 2048;
const SCRIPT_FRAMES = 4096;
// A streamed take sends a chunk once it holds this many output samples (1 s).
const STREAM_CHUNK_SAMPLES = OUT_RATE;
// A suspended context (iOS before a gesture-backed resume) may never run; wait
// this long for it, then give the take to the recorder's container.
const RESUME_WAIT_MS = 1500;
const WORKLET_NAME = 'dictation-pcm';
// The longest stop waits for the worklet's last partial batch.
const DRAIN_WAIT_MS = 300;

// Runs in the AudioWorklet thread: mono-mixes each input block and posts it in
// batches; any message asks for the partial batch, then null as the end marker.
const WORKLET_SOURCE = `
class DictationPcm extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buf = new Float32Array(${BATCH_FRAMES});
    this.at = 0;
    this.port.onmessage = () => {
      if (this.at) this.port.postMessage(this.buf.slice(0, this.at));
      this.at = 0;
      this.port.postMessage(null);
    };
  }
  process(inputs) {
    const chans = inputs[0];
    if (!chans || !chans.length) return true;
    const frames = chans[0].length;
    for (let i = 0; i < frames; i++) {
      let sum = 0;
      for (let c = 0; c < chans.length; c++) sum += chans[c][i];
      this.buf[this.at++] = sum / chans.length;
      if (this.at === this.buf.length) { this.port.postMessage(this.buf); this.buf = new Float32Array(${BATCH_FRAMES}); this.at = 0; }
    }
    return true;
  }
}
registerProcessor('${WORKLET_NAME}', DictationPcm);
`;

// Box-filter resampler: each output sample is the average of the input span it
// covers, which also low-passes the downsample. `push(samples)` returns the
// new output samples; `flush()` returns the partial last one. Pure, so plain
// Node can pin it.
export function createResampler(inRate, outRate) {
  const ratio = inRate / outRate;
  let sum = 0;
  let filled = 0;
  return {
    push: function (samples) {
      const out = [];
      for (let i = 0; i < samples.length; i++) {
        let left = 1;
        while (left > 1e-9) {
          const take = Math.min(left, ratio - filled);
          sum += samples[i] * take;
          filled += take;
          left -= take;
          if (filled >= ratio - 1e-9) {
            out.push(sum / ratio);
            sum = 0;
            filled = 0;
          }
        }
      }
      return out;
    },
    flush: function () {
      if (filled <= 1e-9) return [];
      const last = sum / filled;
      sum = 0;
      filled = 0;
      return [last];
    },
  };
}

// An AudioContext for the take, or null when this browser has no Web Audio.
// Call it in the tap's own turn, before any await: iOS starts a context only
// from a user gesture.
export function openPcmContext() {
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) return null;
  try {
    const ctx = new Ctx();
    if (ctx.state === 'suspended') ctx.resume().catch(function () { /* checked when capture starts */ });
    return ctx;
  } catch (exc) {
    console.info('[dictation] PCM capture off: no audio context (' + ((exc && exc.name) || 'error') + ')');
    return null;
  }
}

// state-literal-exempt: the browser AudioContext's own state vocabulary, not a machine this app owns
const RUNNING = 'running';

async function running(ctx) {
  if (ctx.state === RUNNING) return true;
  const wait = new Promise(function (resolve) { setTimeout(resolve, RESUME_WAIT_MS); });
  try { await Promise.race([ctx.resume(), wait]); } catch (_) { /* judged by the state below */ }
  return ctx.state === RUNNING;
}

// The worklet node on `ctx`, feeding `onBlock`; throws when this browser cannot load one.
async function workletNode(ctx, onBlock) {
  if (!ctx.audioWorklet || !window.AudioWorkletNode) throw new Error('no AudioWorklet');
  const url = URL.createObjectURL(new Blob([WORKLET_SOURCE], { type: 'text/javascript' }));
  try {
    await ctx.audioWorklet.addModule(url);
  } finally {
    URL.revokeObjectURL(url);
  }
  const node = new AudioWorkletNode(ctx, WORKLET_NAME, { numberOfOutputs: 1, outputChannelCount: [1] });
  node.port.onmessage = function (ev) { onBlock(ev.data); };
  // Asks the worklet for the audio it still holds; settles on its end marker.
  node.drain = function () {
    return new Promise(function (resolve) {
      node.drained = resolve;
      node.port.postMessage('drain');
      setTimeout(resolve, DRAIN_WAIT_MS);
    });
  };
  return node;
}

// The ScriptProcessor node on `ctx`: deprecated, but every Web Audio browser has it.
function scriptNode(ctx, onBlock) {
  if (!ctx.createScriptProcessor) throw new Error('no ScriptProcessor');
  const node = ctx.createScriptProcessor(SCRIPT_FRAMES, 1, 1);
  node.onaudioprocess = function (ev) { onBlock(new Float32Array(ev.inputBuffer.getChannelData(0))); };
  return node;
}

// The take's 16 kHz samples as they arrive, and where they go: kept for the
// one WAV `finish()` returns, or, once `streamTo(sink)` is set, handed to the
// sink a second at a time.
function createTake() {
  let chunks = [];
  let held = 0;
  let sink = null;
  let sent = 0;
  let headed = false;
  const held16 = function () {
    const samples = new Float32Array(held);
    let at = 0;
    chunks.forEach(function (c) { samples.set(c, at); at += c.length; });
    chunks = [];
    held = 0;
    return samples;
  };
  const send = function () {
    const body = pcm16Bytes(held16());
    sent += body.length / 2;
    const parts = headed ? [body] : [wavHeader(OUT_RATE, null), body];
    headed = true;
    sink(new Blob(parts, { type: 'audio/wav' }));
  };
  return {
    add: function (samples) {
      if (!samples.length) return;
      chunks.push(samples);
      held += samples.length;
      if (sink && held >= STREAM_CHUNK_SAMPLES) send();
    },
    streamTo: function (fn) {
      sink = fn;
      if (held >= STREAM_CHUNK_SAMPLES) send();
    },
    // The WAV Blob (kept mode) or whether anything was sent (streaming), and
    // null/false when no audio arrived.
    finish: function () {
      if (sink) {
        if (held) send();
        return sent > 0;
      }
      return held ? encodeWav(held16(), OUT_RATE) : null;
    },
  };
}

// Starts capturing `stream` on `ctx` (from openPcmContext). Resolves to
// `{ kind, streamTo(sink), stop() }`, or null when no capture could start (the
// context is closed then). `stop()` ends the capture and resolves to the WAV
// Blob, or, after `streamTo`, to whether any chunk was sent; null/false when no
// audio arrived. `kind` is 'worklet' or 'script'.
export async function startPcmCapture(ctx, stream) {
  if (!ctx) return null;
  const rs = createResampler(ctx.sampleRate, OUT_RATE);
  const take = createTake();
  const onBlock = function (block) {
    if (!block) {
      if (node && node.drained) node.drained();
      return;
    }
    take.add(Float32Array.from(rs.push(block)));
  };
  let source;
  let node;
  let kind = 'worklet';
  try {
    if (!(await running(ctx))) throw new Error('the audio context stayed ' + ctx.state);
    source = ctx.createMediaStreamSource(stream);
    try {
      node = await workletNode(ctx, onBlock);
    } catch (exc) {
      console.info('[dictation] PCM capture: AudioWorklet unavailable (' + ((exc && exc.message) || exc) + '), using ScriptProcessor');
      kind = 'script';
      node = scriptNode(ctx, onBlock);
    }
    // The node must reach the destination or some browsers never run it; a
    // zero-gain stage keeps the mic out of the speakers.
    const mute = ctx.createGain();
    mute.gain.value = 0;
    source.connect(node);
    node.connect(mute);
    mute.connect(ctx.destination);
  } catch (exc) {
    console.info('[dictation] PCM capture could not start: ' + ((exc && exc.message) || exc));
    if (source) source.disconnect();
    closeQuietly(ctx);
    return null;
  }
  return {
    kind: kind,
    streamTo: take.streamTo,
    stop: async function () {
      if (node.drain) await node.drain();
      source.disconnect();
      node.disconnect();
      if (node.port) node.port.onmessage = null;
      node.onaudioprocess = null;
      closeQuietly(ctx);
      take.add(Float32Array.from(rs.flush()));
      return take.finish();
    },
  };
}

// Releases a context that never started a capture (the take was abandoned).
export function closeQuietly(ctx) {
  if (ctx && ctx.state !== 'closed') ctx.close().catch(function () { /* already gone */ });
}

// Ends a capture nobody will upload, so its audio context is released.
export function abandonPcm(capture) {
  if (capture) capture.then(function (h) { return h && h.stop(); }).catch(function () { /* nothing to keep */ });
}
