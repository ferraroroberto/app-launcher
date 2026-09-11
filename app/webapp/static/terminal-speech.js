/* Read-aloud voices for the Coding tab's 🔊 (issues #190, #203, #206),
 * split off `terminal-readback.js` (#884), which re-exports this module's
 * API and keeps the buffer → reply-block extraction that feeds it.
 *
 * Two voices share one button + one speaking-state machine (`setSpeaking` →
 * `onSpeakingChange`/`onSpeechEnd`):
 *  - **Hub TTS (issues #203, #206)** — the preferred path when the
 *    local-llm-hub is reachable: `speakHub()` POSTs the reply to
 *    `/api/tts/speak`, which streams **headerless PCM16** as the hub
 *    synthesizes, and plays it through the **Web Audio API** — first audio in
 *    ~1.5 s instead of waiting for the whole clip. (An `<audio>` element can't
 *    play the hub's open-ended streaming WAV progressively, so Web Audio reads
 *    the PCM stream and schedules it on an AudioContext timeline; the context
 *    is resumed in the click gesture so iOS lets it sound.)
 *  - **Web Speech API (`speechSynthesis`)** — the on-device fallback when the
 *    hub is unconfigured / down / blocks the POST: zero infra, already on iOS
 *    Safari, and the button press supplies the user gesture iOS requires.
 *
 * The 🔊 button tap is the user gesture both paths need (iOS blocks both
 * synthesized speech and `<audio>.play()` outside one) — so `speakHub` unlocks
 * its audio element synchronously, inside the click tick, before its first
 * `await`.
 */

// ── Speech ────────────────────────────────────────────────────────────────

export function isSpeechSupported() {
  return !!(window.speechSynthesis && window.SpeechSynthesisUtterance);
}

let _speaking = false;
let _onStateChange = null;
let _onEnd = null;
let _watchdog = null;
let _observed = false;

function setSpeaking(on) {
  _speaking = on;
  if (_onStateChange) {
    try { _onStateChange(on); } catch (_) { /* UI callback best-effort */ }
  }
}

/** Register a callback fired whenever speaking starts/stops (UI sync). */
export function onSpeakingChange(cb) { _onStateChange = cb; }

/** Register a callback fired once when speech finishes *naturally* (the queue
 *  drained on its own — not a manual stop). */
export function onSpeechEnd(cb) { _onEnd = cb; }

export function isSpeaking() { return _speaking; }

function stopWatchdog() {
  if (_watchdog) { clearInterval(_watchdog); _watchdog = null; }
}

// iOS Safari fires an utterance's `onend` unreliably, so the button could
// stick in its blue "speaking" state forever. Poll the engine instead: once
// we've seen it actually start, both `speaking` and `pending` going false is
// a natural finish.
function startWatchdog() {
  stopWatchdog();
  _observed = false;
  _watchdog = setInterval(function () {
    const s = window.speechSynthesis;
    if (!s) { finishNaturally(); return; }
    if (s.speaking) { _observed = true; return; }
    if (_observed && !s.pending) finishNaturally();
  }, 250);
}

function finishNaturally() {
  stopWatchdog();
  if (!_speaking) return;        // already finalized (e.g. by a manual cancel)
  setSpeaking(false);
  if (_onEnd) { try { _onEnd(); } catch (_) { /* best effort */ } }
}

// Split into short sentence-ish chunks. iOS truncates long single
// utterances, so each sentence becomes its own queued utterance (which also
// makes cancel() responsive mid-reply). Avoids regex lookbehind for older
// iOS Safari.
function chunkForSpeech(text) {
  const rough = text.replace(/([.!?])\s+/g, '$1\n').split(/\n+/);
  const chunks = [];
  for (let i = 0; i < rough.length; i++) {
    const s = rough[i].trim();
    if (!s) continue;
    if (s.length <= 240) {
      chunks.push(s);
    } else {
      for (let j = 0; j < s.length; j += 240) chunks.push(s.slice(j, j + 240));
    }
  }
  return chunks;
}

// Prefer a voice matching the page language, favouring the higher-quality
// "enhanced"/neural voices over the robotic compact default when present.
function pickVoice(synth, lang) {
  let voices = [];
  try { voices = synth.getVoices() || []; } catch (_) { voices = []; }
  if (!voices.length) return null;
  const pref = (lang || 'en').slice(0, 2).toLowerCase();
  const matched = voices.filter(function (v) {
    return (v.lang || '').toLowerCase().indexOf(pref) === 0;
  });
  const pool = matched.length ? matched : voices;
  const enhanced = pool.find(function (v) {
    return !/compact/i.test(v.name || '') &&
      /enhanced|premium|neural|natural|siri/i.test(v.name || '');
  });
  return enhanced || pool.find(function (v) { return v.default; }) || pool[0];
}

/**
 * Speak `text` aloud. Returns false when the browser has no speech synthesis
 * or there is nothing to say.
 *
 * iOS Safari notes baked in here, the hard way:
 *  - **Never call `cancel()` synchronously right before `speak()`** — iOS
 *    silently drops the new utterance. So we only cancel when the engine is
 *    actually busy, and the button handler turns a re-press into a pure
 *    cancel (it never re-enters speak while speaking).
 *  - The `speak()` must run **inside the user-gesture tick** (the button
 *    click) — so no setTimeout, no awaiting voices.
 *  - iOS sometimes starts the queue **paused**; an explicit `resume()` after
 *    queuing kicks it into audible playback.
 */
export function speak(text, opts) {
  const synth = window.speechSynthesis;
  if (!synth || !window.SpeechSynthesisUtterance) return false;
  const chunks = chunkForSpeech(text || '');
  if (!chunks.length) return false;
  // Only clear a genuinely in-flight queue; a blanket cancel() here is what
  // makes the very next speak() silent on iOS.
  if (synth.speaking || synth.pending) {
    try { synth.cancel(); } catch (_) { /* best effort */ }
  }
  const rate = (opts && opts.rate) || 1.3;
  const lang = (opts && opts.lang) || navigator.language || 'en-US';
  const voice = pickVoice(synth, lang);
  setSpeaking(true);
  for (let i = 0; i < chunks.length; i++) {
    const u = new window.SpeechSynthesisUtterance(chunks[i]);
    u.rate = rate;
    u.lang = lang;
    u.volume = 1;
    if (voice) u.voice = voice;
    // Fast-path finish when the last utterance's onend does fire; the
    // watchdog is the reliable backstop on iOS where it often doesn't.
    u.onend = function () {
      if (!synth.pending && !synth.speaking) finishNaturally();
    };
    u.onerror = function () {
      if (!synth.pending && !synth.speaking) finishNaturally();
    };
    synth.speak(u);
  }
  // iOS can leave the engine paused on the first speak after load.
  try { synth.resume(); } catch (_) { /* best effort */ }
  startWatchdog();
  return true;
}

/** Stop any in-flight speech and reset to idle (a manual stop — no end
 *  callback, unlike a natural finish). */
export function cancelSpeech() {
  stopWatchdog();
  const synth = window.speechSynthesis;
  if (synth) { try { synth.cancel(); } catch (_) { /* best effort */ } }
  setSpeaking(false);
}

// ── Hub TTS (issues #203, #206): high-quality Orpheus voice over local-llm-hub ─
// The hub streams *headerless PCM16* (`/api/tts/speak` → `audio/L16` +
// `X-Sample-Rate`) and we play it through the **Web Audio API** — read the
// streaming fetch with getReader(), convert each int16 chunk to float32, and
// schedule AudioBufferSourceNodes back-to-back on an AudioContext timeline.
// This is the technique the hub's own TTS UI uses (first audio in ~1.4 s).
//
// Why not <audio src>: the hub's streaming WAV carries an open-ended RIFF
// header (0xFFFFFFFF sizes) that an <audio> element can't play progressively —
// it just buffers silently (#206). Web Audio sidesteps the container entirely.
// The AudioContext is created + resumed inside the click gesture (before the
// first await) so iOS autoplay policy lets it make sound.

// Single state container for the hub-audio-streaming machine (was 9 loose
// file-scoped `let`s) — still a module-level singleton (one read-aloud surface
// today), but grouped so a future second surface has one object to carry
// instead of a parallel set of globals to remember and keep in sync.
const hub = {
  ctx: null,           // the AudioContext currently rendering hub audio
  reader: null,        // the streaming-body reader (cancelled on stop)
  abort: null,         // AbortController for the in-flight fetch
  endTimer: null,      // fires finishHubNaturally once the last buffer ends
  available: null,     // tri-state: null = unprobed, true / false
  queue: [],           // scheduled buffers {buf,node,start,dur} for re-anchor
  playHead: 0,         // shared scheduling cursor (pump loop + re-anchor)
  hiddenAt: null,      // ctx.currentTime captured when the page went hidden
  streamDone: false,   // true once the PCM stream is fully read + scheduled
  visHandler: null,    // visibilitychange listener (removed on teardown)
  lastGain: null,      // gain node of the most recently scheduled buffer …
  lastEnd: 0,          // … and its end time — for the end-of-stream tail fade
};

// Bearer + passkey terminal token, supplied by the caller (terminal.js owns
// the token plumbing; this module stays free of api.js / webauthn.js
// imports). Header casing (`X-Terminal-Token`) matches api.js's authHeaders()
// — kept as a separate local copy here, not an import, by design.
function authHeaders(opts) {
  const h = {};
  if (opts && opts.token) h['Authorization'] = 'Bearer ' + opts.token;
  if (opts && opts.terminalToken) h['X-Terminal-Token'] = opts.terminalToken;
  return h;
}

/** Probe whether the hub's read-aloud voice is reachable right now and cache
 *  the result. Returns the boolean; never throws (a failure caches false). */
export async function probeHub(opts) {
  try {
    const res = await fetch('/api/tts/health', { headers: authHeaders(opts) });
    if (!res.ok) { hub.available = false; return false; }
    const body = await res.json().catch(function () { return null; });
    hub.available = !!(body && body.available);
  } catch (_) {
    hub.available = false;
  }
  return hub.available;
}

/** True once a probe has confirmed the hub voice is reachable. */
export function isHubAvailable() { return hub.available === true; }

// Tear down all hub-playback resources (timer, reader, fetch, AudioContext).
// Closing the context silences any still-scheduled buffers immediately.
function hubTeardown() {
  if (hub.endTimer) { clearTimeout(hub.endTimer); hub.endTimer = null; }
  if (hub.visHandler) {
    try { document.removeEventListener('visibilitychange', hub.visHandler); }
    catch (_) { /* best effort */ }
    hub.visHandler = null;
  }
  hub.queue = [];
  hub.hiddenAt = null;
  hub.streamDone = false;
  hub.lastGain = null;
  hub.lastEnd = 0;
  if (hub.reader) {
    try { hub.reader.cancel(); } catch (_) { /* best effort */ }
    hub.reader = null;
  }
  if (hub.abort) {
    try { hub.abort.abort(); } catch (_) { /* best effort */ }
    hub.abort = null;
  }
  if (hub.ctx) {
    const ctx = hub.ctx;
    hub.ctx = null;
    try { ctx.close(); } catch (_) { /* best effort */ }
  }
}

function finishHubNaturally() {
  hubTeardown();
  if (!_speaking) return;       // already finalized (e.g. by a manual cancel)
  setSpeaking(false);
  if (_onEnd) { try { _onEnd(); } catch (_) { /* best effort */ } }
}

// Screen-lock / backgrounding robustness (issue #248). iOS leaves the
// AudioContext `running` through a short lock — its `currentTime` clock keeps
// advancing — but SUSPENDS actual output. Buffers scheduled on the absolute
// `ctx.currentTime` timeline whose start elapsed during the lock are "started
// in the past" → silently dropped, so the read-aloud tail is clipped by ~the
// locked duration. The fix: remember where the clock was when the page went
// hidden, and on resume re-anchor every buffer that hadn't finished playing by
// then to `ctx.currentTime`, contiguously — so no scheduled buffer is lost.
function installHubVisibility() {
  if (hub.visHandler) return;
  hub.visHandler = function () {
    if (!hub.ctx) return;
    if (document.visibilityState === 'hidden') {
      hub.hiddenAt = hub.ctx.currentTime;        // last audible position
    } else if (document.visibilityState === 'visible') {
      reanchorHubQueue();
    }
  };
  try { document.addEventListener('visibilitychange', hub.visHandler); }
  catch (_) { /* best effort */ }
}

// Re-schedule the un-played tail after an output suspension. `hub.hiddenAt` is
// the clock position when output stopped; any buffer whose playback window
// extended past it was dropped or cut short by the suspension and is replayed,
// contiguously, from "now". A no-op when nothing was hidden / no buffer remains.
function reanchorHubQueue() {
  const ctx = hub.ctx;
  if (!ctx) return;
  const boundary = hub.hiddenAt;
  hub.hiddenAt = null;
  if (boundary == null) return;
  // Buffers that fully sounded before the suspension are done; keep the rest.
  const pending = hub.queue.filter(function (e) {
    return e.start + e.dur > boundary;
  });
  if (!pending.length) return;
  // Cancel the stale nodes (dropped ones are already silent; a mid-play or
  // still-future node is replaced so the tail stays gap-free and unduplicated).
  for (let i = 0; i < pending.length; i++) {
    try { pending[i].node.stop(); } catch (_) { /* already ended */ }
    pending[i].node.onended = null;
  }
  let playHead = ctx.currentTime + 0.05;       // small lead-in after the gap
  const fresh = [];
  let lastNode = null;
  for (let i = 0; i < pending.length; i++) {
    if (playHead < ctx.currentTime + 0.02) playHead = ctx.currentTime + 0.02;
    // Only the first re-anchored buffer sits on a real gap (the suspension);
    // the rest are contiguous with it and must not re-fade.
    const node = scheduleHubBuffer(ctx, pending[i].buf, playHead, i === 0);
    fresh.push({ buf: pending[i].buf, node: node, start: playHead, dur: pending[i].dur });
    playHead += pending[i].dur;
    lastNode = node;
  }
  hub.queue = fresh;
  hub.playHead = playHead;
  // Re-arm the natural-finish only when the stream is fully read; if the pump
  // loop is still running it owns the finish on the true last buffer.
  if (hub.endTimer) { clearTimeout(hub.endTimer); hub.endTimer = null; }
  if (hub.streamDone && lastNode) {
    lastNode.onended = function () { finishHubNaturally(); };
    const ms = Math.max(0, (playHead - ctx.currentTime) * 1000) + 1500;
    hub.endTimer = setTimeout(function () { finishHubNaturally(); }, ms);
  }
}

// A silence↔audio discontinuity (real silence jumping straight to a
// mid-amplitude sample, or playback running out mid-waveform) is an audible
// click — and that's exactly what happens every time delivery falls behind and
// guard 1 below snaps `playHead` forward to "now" (issue #599): the prior
// buffer's tail played out, real silence followed, and the next buffer starts
// at full amplitude with no ramp. The fade is applied ONLY at such
// discontinuity edges (stream/segment start, a guard-1 snap, a post-lock
// re-anchor) plus one tail fade-out on the true final buffer: contiguous
// buffers carry sample-continuous PCM, and fading every one of them would
// dip the level to zero at each ~85 ms SNAC-window seam — an audible flutter
// far worse than the clicks being fixed. (4 ms is far below the ~10 ms
// auditory threshold for a level change.)
const HUB_FADE_S = 0.004;

// Create + start an `AudioBufferSourceNode` for `buf` at `start`, routed
// through a per-node `GainNode`; when `fadeIn` is set (a discontinuity edge)
// the buffer ramps in from silence. The gain of the most recent node is kept
// on `hub.lastGain`/`hub.lastEnd` so the pump can add the single end-of-stream
// tail fade once it knows which buffer is last. Returns the source node
// (callers track it exactly as before).
function scheduleHubBuffer(ctx, buf, start, fadeIn) {
  const node = ctx.createBufferSource();
  node.buffer = buf;
  const gain = ctx.createGain();
  node.connect(gain);
  gain.connect(ctx.destination);
  const dur = buf.duration;
  const fade = Math.min(HUB_FADE_S, dur / 2);
  if (fadeIn && fade > 0) {
    gain.gain.setValueAtTime(0, start);
    gain.gain.linearRampToValueAtTime(1, start + fade);
  }
  hub.lastGain = gain;
  hub.lastEnd = start + dur;
  node.start(start);
  return node;
}

// Stream PCM16 from `res` and schedule it on `ctx`'s timeline. Resolves once the
// WHOLE stream has been read + scheduled; the audio keeps playing until the last
// buffer's end, after which `finishHubNaturally` fires. Mirrors local-llm-hub's
// playground.js speakStream.
//
// Orpheus can synthesize slower than realtime, so the stream may arrive slower
// than it plays. Three guards keep playback smooth (issue #206 follow-up,
// #599 follow-up):
//  1. never schedule a buffer in the *past* — if delivery fell behind, resume
//     `playHead` from "now", so it always tracks the true end of audio (a buffer
//     started in the past would otherwise make playHead under-count the end and
//     overlap earlier audio);
//  2. finish on the LAST buffer's `onended` (the real end), with a generous
//     timer only as a backstop — the previous timer-only finish, computed from a
//     drifted playHead, fired early and `ctx.close()` chopped the tail; and
//  3. a short gain fade-in is applied ONLY where a real silence↔audio edge
//     exists — the first buffer of a segment and any buffer scheduled right
//     after a guard-1 snap — plus one tail fade-out on the final buffer;
//     contiguous buffers stay un-faded so their sample-continuous seams are
//     untouched (per-buffer fades would flutter, see HUB_FADE_S above).
//
// `seg` carries a segment's position within a multi-request read (issue #254):
// the hub caps each synthesis request at ~49.6 s of audio, so a long reply is
// streamed as several back-to-back POSTs onto this *one* timeline. Only the
// FIRST segment resets the playHead / queue (and installs the lock re-anchor);
// only the LAST arms the natural finish — the segments in between simply append
// their buffers to the shared timeline and return. A single-shot read omits
// `seg`, so both flags default true and the path is unchanged.
async function pumpPcmStream(ctx, res, ac, seg) {
  const isFirst = !seg || seg.isFirst !== false;
  const isLast = !seg || seg.isLast !== false;
  // Recover the context if iOS suspended/interrupted it during the await(s)
  // between the gesture and now — without this, a long gap (the summarize
  // path waits on the LLM first) leaves the context silent (issue #210).
  try { ctx.resume(); } catch (_) { /* best effort */ }
  const sampleRate =
    parseInt(res.headers.get('X-Sample-Rate') || '24000', 10) || 24000;
  if (isFirst) {
    // Lead-in cushion against underrun (issue #599: Orpheus can run well
    // below realtime under GPU load, so a bigger cushion buys more time
    // before the first catch-up gap than the previous 0.15s did).
    hub.playHead = ctx.currentTime + 0.35;
    hub.queue = [];
    hub.hiddenAt = null;
    installHubVisibility();   // re-anchor the tail if iOS suspends output (lock)
  }
  // Not finished until the FINAL segment is fully scheduled; an intermediate
  // segment keeps the stream "open" so the lock re-anchor doesn't fire finish.
  hub.streamDone = false;
  let leftover = new Uint8Array(0);
  // Each segment starts on a real edge (silence, or another synthesis call's
  // tail) → fade its first buffer in; reset to false once scheduled, and set
  // again whenever guard 1 below snaps over a genuine delivery gap.
  let discontinuity = true;
  const reader = res.body.getReader();
  hub.reader = reader;
  for (;;) {
    const chunk = await reader.read();
    if (chunk.done) break;
    if (ac.signal.aborted) return;
    const value = chunk.value;
    if (!value || value.length === 0) continue;
    // Merge any odd trailing byte from the previous chunk, then split into whole
    // int16 samples (carry the remainder forward).
    const merged = new Uint8Array(leftover.length + value.length);
    merged.set(leftover, 0);
    merged.set(value, leftover.length);
    const usable = merged.length - (merged.length % 2);
    leftover = merged.slice(usable);
    if (usable === 0) continue;
    const i16 = new Int16Array(merged.buffer.slice(0, usable));
    const f32 = new Float32Array(i16.length);
    for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;
    const buf = ctx.createBuffer(1, f32.length, sampleRate);
    buf.copyToChannel(f32, 0);
    // Guard 1: never start in the past — keeps playHead == true end of audio.
    if (hub.playHead < ctx.currentTime + 0.02) {
      hub.playHead = ctx.currentTime + 0.02;
      discontinuity = true;   // a real silent gap just played out
    }
    const node = scheduleHubBuffer(ctx, buf, hub.playHead, discontinuity);
    discontinuity = false;
    // Track every scheduled buffer so a screen-lock suspension (#248) can
    // re-anchor the un-played tail instead of losing it.
    hub.queue.push({ buf: buf, node: node, start: hub.playHead, dur: buf.duration });
    hub.playHead += buf.duration;
  }
  if (!isLast) return;   // more segments still to stream onto this timeline
  hub.streamDone = true;
  if (!hub.queue.length) { finishHubNaturally(); return; }
  // Tail fade-out on the true final buffer — only knowable now that the whole
  // stream is scheduled (mid-stream buffers never fade out; see HUB_FADE_S).
  if (hub.lastGain && hub.lastEnd > ctx.currentTime + HUB_FADE_S) {
    try {
      hub.lastGain.gain.setValueAtTime(1, hub.lastEnd - HUB_FADE_S);
      hub.lastGain.gain.linearRampToValueAtTime(0, hub.lastEnd);
    } catch (_) { /* best effort — a click here beats a crash */ }
  }
  // Guard 2: finish when the final buffer actually ends; the timer only backs
  // it up (with ample slack) in case onended doesn't fire. The true last buffer
  // is the tail of the shared queue (across all segments), not this segment's.
  const finalNode = hub.queue[hub.queue.length - 1].node;
  finalNode.onended = function () { finishHubNaturally(); };
  const ms = Math.max(0, (hub.playHead - ctx.currentTime) * 1000) + 1500;
  hub.endTimer = setTimeout(function () { finishHubNaturally(); }, ms);
}

// The hub's Orpheus engine hard-caps each synthesis request at n_predict:4096
// SNAC tokens ≈ 49.6 s of audio (local-llm-hub tts_engines.py); a longer reply
// is silently truncated server-side on an HTTP-200 stream, so the verbatim
// read-aloud of any non-trivial reply loses everything past ~49.6 s (issue
// #254). Until the hub itself chunks (the primary fix — see the cross-linked
// local-llm-hub issue), the verbatim path defends in depth by splitting the
// reply into segments that each synthesize well under the cap and streaming
// them back-to-back on one Web Audio timeline. Budget: ~18 chars/s of speech
// measured at default speed (67 chars → 3.67 s), so the 49.6 s cap ≈ 900 chars;
// 700 (~38 s) leaves headroom for slower `speed` settings and punctuation.
const HUB_SEGMENT_CHARS = 700;

// Split `text` into ≤HUB_SEGMENT_CHARS segments for back-to-back hub synthesis,
// breaking on sentence boundaries (like `chunkForSpeech` for the Web-Speech
// path) and greedily packing whole sentences up to the budget. A single
// sentence longer than the budget is hard-split, preferring the last space so a
// word isn't cut mid-token. Returns [] for empty input, [text] when it already
// fits (the common short-reply case → one request, unchanged behaviour).
export function chunkForHub(text) {
  const clean = (text || '').trim();
  if (!clean) return [];
  if (clean.length <= HUB_SEGMENT_CHARS) return [clean];
  const sentences = clean.replace(/([.!?])\s+/g, '$1\n').split(/\n+/);
  const segments = [];
  let cur = '';
  const push = function (s) { const t = (s || '').trim(); if (t) segments.push(t); };
  for (let i = 0; i < sentences.length; i++) {
    let s = sentences[i].trim();
    if (!s) continue;
    // An oversized lone sentence: flush the pending segment, then carve budget-
    // sized chunks off the front, breaking at the last space within budget.
    while (s.length > HUB_SEGMENT_CHARS) {
      if (cur) { push(cur); cur = ''; }
      let cut = s.lastIndexOf(' ', HUB_SEGMENT_CHARS);
      if (cut <= 0) cut = HUB_SEGMENT_CHARS;
      push(s.slice(0, cut));
      s = s.slice(cut).trim();
    }
    if (!s) continue;
    if (!cur) cur = s;
    else if (cur.length + 1 + s.length <= HUB_SEGMENT_CHARS) cur += ' ' + s;
    else { push(cur); cur = s; }
  }
  push(cur);
  return segments;
}

/**
 * Speak `text` through the hub's Orpheus voice, played progressively via Web
 * Audio. Resolves true once the stream has been consumed (audio keeps playing
 * until its scheduled end); **rejects** on any failure (hub unconfigured /
 * down / blocked POST / no Web Audio) so the caller can fall back to `speak()`.
 * A manual cancel is NOT a rejection.
 *
 * `opts`: `{ token, terminalToken, voice, speed }`. Must be called directly
 * inside the button-click handler — the synchronous prologue (before the first
 * `await`) creates + resumes the AudioContext within the user gesture iOS
 * requires.
 */
export async function speakHub(text, opts) {
  const clean = (text || '').trim();
  if (!clean) return false;
  const handle = prepareHub();     // synchronous gesture-tick prologue
  return speakHubInto(handle, clean, opts);
}

/**
 * Synchronous prologue for hub playback — MUST run inside the button-click
 * gesture tick. Cancels any in-flight read-aloud, creates + resumes a fresh
 * AudioContext (iOS only lets audio sound when the context is unlocked inside a
 * user gesture), arms an AbortController, and flips the speaking state on so the
 * button shows ⏹ immediately. Returns a `{ ctx, ac }` handle for
 * `speakHubInto`. Split out (issue #210) so the "summarize & read" path can
 * unlock audio in the gesture, then `await` the summary, then stream into this
 * context — keeping iOS autoplay happy across the network round-trip. Throws
 * when Web Audio is unavailable (the caller falls back to Web Speech).
 */
export function prepareHub() {
  cancelHub();                 // a second press / restart cancels in-flight audio
  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  if (!AudioCtx) throw new Error('Web Audio API unavailable');
  const ctx = new AudioCtx();
  hub.ctx = ctx;
  try { ctx.resume(); } catch (_) { /* best effort */ }
  // iOS unlock: play one silent sample NOW, inside the gesture, so the context
  // is genuinely user-activated. iOS only "blesses" a context that produces
  // output within the gesture's activation window — the summarize path's real
  // audio arrives seconds later (after the LLM round-trip), too late to unlock
  // on its own, so a context created-but-silent stays muted without this (#210).
  try {
    const silent = ctx.createBuffer(1, 1, ctx.sampleRate || 22050);
    const src = ctx.createBufferSource();
    src.buffer = silent;
    src.connect(ctx.destination);
    src.start(0);
  } catch (_) { /* best effort */ }
  const ac = new AbortController();
  hub.abort = ac;
  setSpeaking(true);
  return { ctx, ac };
}

/**
 * Async body for hub playback: POST `text` to `/api/tts/speak` and stream the
 * PCM into the prepared `handle.ctx`. Resolves true once the stream is consumed
 * (audio keeps playing to its scheduled end); **rejects** on any failure so the
 * caller can fall back to `speak()`. A manual cancel is NOT a rejection.
 * `handle` comes from `prepareHub()` (called in the gesture tick).
 */
export async function speakHubInto(handle, text, opts) {
  const { ctx, ac } = handle;
  // Split a long reply into sub-cap segments streamed back-to-back on the one
  // timeline (issue #254). A short reply yields a single segment → one POST,
  // identical to the previous behaviour.
  const segments = chunkForHub(text);
  if (!segments.length) { finishHubNaturally(); return true; }
  for (let i = 0; i < segments.length; i++) {
    if (ac.signal.aborted) return true;   // cancelled between segments — done
    try {
      const res = await fetch('/api/tts/speak', {
        method: 'POST',
        headers: Object.assign(
          { 'Content-Type': 'application/json' }, authHeaders(opts)
        ),
        body: JSON.stringify({
          text: segments[i],
          voice: (opts && opts.voice) || undefined,
          speed: (opts && opts.speed) || undefined,
        }),
        signal: ac.signal,
      });
      if (!res.ok) {
        // .status lets a caller that imports api.js (e.g. terminal-readaloud.js)
        // recognise a 401 and reopen the login overlay (issue #333) — this
        // module stays free of api.js imports itself (see authHeaders() above),
        // so it can only tag the Error, not call showLogin() directly.
        const err = new Error('hub tts HTTP ' + res.status);
        err.status = res.status;
        throw err;
      }
      if (!res.body || !res.body.getReader) throw new Error('hub tts stream unsupported');
      await pumpPcmStream(ctx, res, ac, {
        isFirst: i === 0, isLast: i === segments.length - 1,
      });
    } catch (err) {
      if (ac.signal.aborted) return true;  // cancelled — not a failure, no fallback
      if (i === 0) {
        // Nothing has sounded yet: tear down and reject so the caller falls back
        // to Web Speech for the whole reply (preserves the single-shot contract).
        // Only if this call still owns the shared state — a newer speakHub() may
        // have aborted us and taken over (don't clobber it).
        if (hub.abort === ac) cancelHub();
        throw err;
      }
      // A later segment failed AFTER earlier ones already sounded. Restarting the
      // whole reply via Web Speech would re-read what the user just heard, so
      // finish gracefully on what played rather than reject into the fallback.
      finishHubNaturally();
      return true;
    }
  }
  return true;
}

/**
 * Ask the hub to summarize `text` for driving (issue #210): POST it to
 * `/api/tts/summarize`, which routes to the hub's `claude-haiku-4-5`, and
 * resolve the short summary string. Rejects on any failure (hub unconfigured /
 * down / blocked POST / empty completion) so the caller can surface an error
 * and skip the read. `opts`: `{ token, terminalToken }`.
 */
export async function summarizeReply(text, opts) {
  const clean = (text || '').trim();
  if (!clean) throw new Error('nothing to summarize');
  const res = await fetch('/api/tts/summarize', {
    method: 'POST',
    headers: Object.assign(
      { 'Content-Type': 'application/json' }, authHeaders(opts)
    ),
    body: JSON.stringify({ text: clean }),
  });
  if (!res.ok) {
    // .status lets a caller that imports api.js recognise a 401 and reopen
    // the login overlay (issue #333) — see the matching note in speakHubInto.
    const err = new Error('hub summarize HTTP ' + res.status);
    err.status = res.status;
    throw err;
  }
  const body = await res.json().catch(function () { return null; });
  const summary = body && typeof body.summary === 'string' ? body.summary.trim() : '';
  if (!summary) throw new Error('empty summary');
  return summary;
}

/** Stop any in-flight hub read-aloud and reset to idle (a manual stop). */
export function cancelHub() {
  hubTeardown();
  setSpeaking(false);
}
