/* Spike #246 — controller for the hands-free voice loop prototype. THROWAWAY.
 *
 * Wires real audio + network to the pure state machine in
 * spike-voice-loop-fsm.js and instruments the four viability questions the
 * spike must answer on a real iPhone (the doc, docs/voice-loop-spike.md, has
 * the full framing):
 *
 *   1. Can the mic stay open / re-arm across turns with no per-turn tap?
 *   2. Can synthesized narration play back-to-back without a fresh gesture?
 *   3. Does barge-in work (user speaks → narration stops)?
 *   4. Does the loop survive screen-lock / backgrounding?
 *
 * Per the spike's chosen shape (#246): the LISTEN side is real — continuous
 * VAD over one long-lived getUserMedia stream → single-shot whisper
 * (/api/transcribe) — so re-arm and "speak → understood" latency are measured
 * for real. The NARRATION is a **pure mock**: a rotating canned board-state
 * line, played through the shipped hub-TTS path (terminal-readback.js) so
 * back-to-back playback + gesture-gating are measured for real, with no
 * orchestrator (#245) in the loop. Only the "brain" is stubbed.
 *
 * The headline instrument is the forced-gesture counter: a turn that re-arms
 * the mic and plays narration on its own is hands-free; a turn that needs a
 * tap is not. iOS gates audio playback and mic capture behind per-action user
 * gestures, so the open question is whether the single Start tap's blessing
 * survives turn after turn, or decays — measurable only on the device, which
 * is why this prototype reports numbers on-screen rather than guessing.
 *
 * Auth reuses the production plumbing exactly: bearer token (api.js) + passkey
 * terminal token (webauthn.js), the same pair terminal.js sends to
 * /api/transcribe and /api/tts/speak.
 */

import { VoiceLoop, STATE, INTENT } from './spike-voice-loop-fsm.js';
import { tokenFromUrl, writeToken, readToken, jsonApi } from './api.js';
import { ensureTerminalToken, readTerminalToken } from './webauthn.js';
import { state } from './state.js';
import {
  prepareHub, speakHubInto, speak, cancelHub, cancelSpeech,
  probeHub, isHubAvailable, onSpeechEnd, onSpeakingChange,
} from './terminal-readback.js';

// Expose the pure FSM for the off-device loop-logic test (no audio/network).
if (typeof window !== 'undefined') {
  window.__voiceloop = { VoiceLoop, STATE, INTENT };
}

// ── VAD + capture tuning (energy-based voice-activity detection) ─────────────
const VAD_POLL_MS = 50;        // analyser sampling cadence
const SPEECH_RMS = 0.025;      // RMS above this = voice present
const MIN_SPEECH_MS = 250;     // ignore sub-250 ms blips (clicks, taps)
const SILENCE_HANG_MS = 900;   // trailing silence that ends a take
const MAX_CAPTURE_MS = 15000;  // hard cap on one take — never wedge in capturing
const BARGE_MS = 350;          // sustained voice during narration = barge-in

// Canned board-state narration (the mock brain). Rotates per turn so each
// playback is a fresh, realistic-length utterance.
const NARRATION_LINES = [
  'Three sessions working. photo-ocr needs you on the chunk-merge question. The app-launcher pull request is green and waiting.',
  'reporting finished its run and posted to Slack. voice-transcriber is idle. life-os has two skills queued.',
  'The fleet map regenerated. grocery-shopping hit a captcha and paused. Everything else is healthy.',
  'claude-local-calls is serving on port eight thousand. The hub TTS voice is reachable. No errors in the last hour.',
];

const loop = new VoiceLoop();
let opts = { token: '', terminalToken: '' };
let micStream = null;
let audioCtx = null;
let analyser = null;
let vadData = null;
let vadTimer = null;
let recorder = null;
let recChunks = [];
let recMime = '';
let narrationIdx = 0;
let useHub = false;
let running = false;

// VAD running state. The FSM's state (LISTENING vs CAPTURING) is the source
// of truth for "are we recording"; these just time the take + the barge-in.
let speechStartAt = 0;
let lastVoiceAt = 0;
let bargeVoiceStart = 0;

// ── tiny DOM helpers ─────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
function setText(id, v) { const el = $(id); if (el) el.textContent = String(v); }
function nowMs() { return (window.performance && performance.now) ? performance.now() : Date.now(); }

function log(msg, cls) {
  const box = $('eventLog');
  if (!box) return;
  const line = document.createElement('div');
  if (cls) line.className = cls;
  const t = new Date().toLocaleTimeString();
  line.textContent = `${t}  ${msg}`;
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}

function renderMetrics() {
  const m = loop.metrics;
  setText('mState', loop.state);
  setText('mTurns', m.turns);
  setText('mForced', m.forcedGestures);
  setText('mBarge', m.bargeIns);
  setText('mTranscripts', m.transcripts);
  setText('mSpeechToText',
    m.lastSpeechToTranscriptMs == null ? '—' : Math.round(m.lastSpeechToTranscriptMs) + ' ms');
  setText('mTextToAudio',
    m.lastTranscriptToAudioMs == null ? '—' : Math.round(m.lastTranscriptToAudioMs) + ' ms');
  // Forced-gesture banner: the headline. Red the moment iOS makes us tap.
  const banner = $('verdict');
  if (banner) {
    if (m.forcedGestures > 0) {
      banner.textContent =
        `⚠️ ${m.forcedGestures} forced tap(s) across ${m.turns} turn(s) — not fully hands-free`;
      banner.className = 'verdict bad';
    } else if (m.turns > 0) {
      banner.textContent = `✅ ${m.turns} turn(s) hands-free so far — no forced taps`;
      banner.className = 'verdict good';
    } else {
      banner.textContent = 'Awaiting first turn…';
      banner.className = 'verdict';
    }
  }
}

// ── intent dispatcher ────────────────────────────────────────────────────────
// Every FSM event returns an intent; this performs the audio/network side
// effect for it. Chained intents (e.g. onNarrationEnd → ARM_LISTEN) route here.
function apply(intent) {
  renderMetrics();
  switch (intent) {
    case INTENT.ARM_LISTEN: armListen(); break;
    case INTENT.START_CAPTURE: startCapture(); break;
    case INTENT.TRANSCRIBE: transcribe(); break;
    case INTENT.NARRATE: narrate(); break;
    case INTENT.STOP_NARRATION: stopNarration(); armListen(); break;
    case INTENT.PAUSE: showResume(true); break;
    case INTENT.STOP_ALL: teardown(); break;
    default: break;
  }
}

// ── listen / VAD ─────────────────────────────────────────────────────────────
function armListen() {
  bargeVoiceStart = 0;
  log('👂 listening (mic re-armed, no tap)', 'ok');
  renderMetrics();
}

function vadTick() {
  if (!analyser || !vadData) return;
  analyser.getFloatTimeDomainData(vadData);
  let sum = 0;
  for (let i = 0; i < vadData.length; i++) sum += vadData[i] * vadData[i];
  const rms = Math.sqrt(sum / vadData.length);
  const t = nowMs();
  const voiced = rms > SPEECH_RMS;

  if (loop.state === STATE.NARRATING) {
    // Barge-in: sustained voice over the narration cuts it.
    if (voiced) {
      if (!bargeVoiceStart) bargeVoiceStart = t;
      else if (t - bargeVoiceStart >= BARGE_MS) {
        log('🙋 barge-in detected — stopping narration', 'warn');
        bargeVoiceStart = 0;
        apply(loop.onBargeIn());
      }
    } else {
      bargeVoiceStart = 0;
    }
    return;
  }

  if (loop.state === STATE.LISTENING) {
    // Waiting for speech to begin. First sustained voice opens the take.
    if (voiced) {
      lastVoiceAt = t;
      speechStartAt = t;
      apply(loop.onSpeechStart());   // → CAPTURING
    }
    return;
  }

  if (loop.state === STATE.CAPTURING) {
    // Recording the take. Trailing silence (or the hard cap) ends it — this
    // is what was missing: end-of-speech must be detected *while* capturing,
    // not while listening, or the take never closes.
    if (voiced) {
      lastVoiceAt = t;
    } else if ((t - lastVoiceAt) > SILENCE_HANG_MS &&
               (t - speechStartAt) > MIN_SPEECH_MS) {
      apply(loop.onSpeechEnd(nowMs()));   // → TRANSCRIBE
    }
    if ((t - speechStartAt) > MAX_CAPTURE_MS) {
      log('⏱️ max capture reached — closing take', 'warn');
      apply(loop.onSpeechEnd(nowMs()));
    }
  }
}

// ── capture / transcribe ─────────────────────────────────────────────────────
function pickAudioMime() {
  const MR = window.MediaRecorder;
  if (!MR || !MR.isTypeSupported) return '';
  const ladder = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4;codecs=mp4a.40.2', 'audio/mp4'];
  for (const c of ladder) if (MR.isTypeSupported(c)) return c;
  return '';
}

function startCapture() {
  if (!micStream) return;
  recChunks = [];
  try {
    recMime = pickAudioMime();
    recorder = recMime ? new MediaRecorder(micStream, { mimeType: recMime })
                       : new MediaRecorder(micStream);
  } catch (exc) {
    log('❌ recorder failed: ' + (exc.message || exc), 'err');
    return;
  }
  recorder.addEventListener('dataavailable', (ev) => {
    if (ev.data && ev.data.size) recChunks.push(ev.data);
  });
  recorder.start();
  log('🎙️ capturing take…');
}

function authHeaders() {
  const h = new Headers();
  if (opts.token) h.set('Authorization', 'Bearer ' + opts.token);
  if (opts.terminalToken) h.set('X-Terminal-Token', opts.terminalToken);
  return h;
}

function transcribe() {
  if (!recorder) { apply(loop.onTranscript('', nowMs())); return; }
  const rec = recorder;
  recorder = null;
  rec.addEventListener('stop', async () => {
    const type = rec.mimeType || recMime || 'audio/webm';
    const blob = new Blob(recChunks, { type });
    recChunks = [];
    if (!blob.size) { apply(loop.onTranscript('', nowMs())); return; }
    const ext = type.indexOf('mp4') >= 0 ? 'mp4' : 'webm';
    const fd = new FormData();
    fd.append('file', blob, 'take.' + ext);
    try {
      const res = await fetch('/api/transcribe', { method: 'POST', headers: authHeaders(), body: fd });
      const body = await res.json().catch(() => null);
      if (!res.ok) throw new Error((body && body.detail) || ('HTTP ' + res.status));
      const text = (body && body.transcript) || '';
      log('📝 transcript: ' + (text || '(silent)'), 'ok');
      setText('lastTranscript', text || '(silent)');
      apply(loop.onTranscript(text, nowMs()));
    } catch (exc) {
      log('❌ transcribe failed: ' + (exc.message || exc), 'err');
      // Treat as an empty turn so the loop keeps cycling rather than wedging.
      apply(loop.onTranscript('', nowMs()));
    }
  });
  try { rec.stop(); } catch (_) { apply(loop.onTranscript('', nowMs())); }
}

// ── narrate (mock brain → real TTS playback) ─────────────────────────────────
function nextNarration() {
  const line = NARRATION_LINES[narrationIdx % NARRATION_LINES.length];
  narrationIdx += 1;
  return line;
}

async function narrate() {
  const line = nextNarration();
  setText('lastNarration', line);
  log('🔊 narrating: ' + line.slice(0, 48) + '…');

  if (!useHub) {
    // Web Speech fallback — onSpeechEnd (registered once) fires the turn end.
    // Web Speech doesn't expose a context state we can probe, so a silent
    // failure here shows up as a stalled turn rather than a clean forced-tap.
    apply(loop.onNarrationStart(nowMs()));
    speak(line, { rate: 1.2 });
    return;
  }

  // Hub TTS via Web Audio. prepareHub() creates + tries to bless an
  // AudioContext; OUTSIDE a user gesture (every turn after the first) iOS
  // leaves it 'suspended'. That suspension IS the spike's failure signal:
  // detect it and demand a fresh tap rather than narrating into silence.
  let handle;
  try {
    handle = prepareHub();
  } catch (exc) {
    log('❌ hub prepare failed: ' + (exc.message || exc), 'err');
    apply(loop.onGestureRequired(INTENT.NARRATE));
    return;
  }
  try { await handle.ctx.resume(); } catch (_) { /* best effort */ }
  if (handle.ctx.state !== 'running') {
    log('🚫 AudioContext suspended outside a gesture — iOS wants a tap', 'warn');
    try { cancelHub(); } catch (_) { /* best effort */ }
    apply(loop.onGestureRequired(INTENT.NARRATE));
    return;
  }
  apply(loop.onNarrationStart(nowMs()));
  try {
    await speakHubInto(handle, line, opts);
    // onSpeechEnd (registered once in start()) fires onNarrationEnd at the
    // real end of audio — don't end the turn here, the stream is still playing.
  } catch (exc) {
    log('❌ hub playback failed: ' + (exc.message || exc), 'err');
    apply(loop.onGestureRequired(INTENT.NARRATE));
  }
}

function stopNarration() {
  try { cancelHub(); } catch (_) { /* best effort */ }
  try { cancelSpeech(); } catch (_) { /* best effort */ }
}

// ── forced-gesture pause / resume ────────────────────────────────────────────
function showResume(on) {
  const btn = $('resumeBtn');
  if (btn) btn.hidden = !on;
  if (on) log('⏸️ paused — tap Resume (iOS forced a gesture)', 'warn');
}

async function onResume() {
  showResume(false);
  // The Resume tap is the fresh gesture. Bless a context inside it so the
  // resumed narration can actually sound, then hand control back to the FSM.
  if (useHub) {
    try { prepareHub(); } catch (_) { /* best effort */ }
  }
  apply(loop.onGestureResumed());
}

// ── lifecycle instrumentation (question 4: screen-lock / backgrounding) ──────
function snapshot() {
  const ctxState = audioCtx ? audioCtx.state : 'none';
  const track = micStream ? (micStream.getAudioTracks()[0] || null) : null;
  const mic = track ? `${track.readyState}${track.muted ? '/muted' : ''}` : 'none';
  return `ctx=${ctxState} mic=${mic} state=${loop.state}`;
}

function wireLifecycle() {
  const ev = (name) => log(`📱 ${name} — ${snapshot()}`, 'life');
  document.addEventListener('visibilitychange',
    () => log(`📱 visibility=${document.visibilityState} — ${snapshot()}`, 'life'));
  window.addEventListener('pagehide', () => ev('pagehide'));
  window.addEventListener('pageshow', () => ev('pageshow'));
  // Page Lifecycle API (Chromium/iOS): the loop's real screen-lock test.
  window.addEventListener('freeze', () => ev('freeze'), { capture: true });
  window.addEventListener('resume', () => ev('resume'), { capture: true });
}

// ── start / stop ─────────────────────────────────────────────────────────────
async function start() {
  if (running) return;
  $('startBtn').disabled = true;
  log('▶️ start tapped — the single gesture that must carry the whole loop');

  // 1) Auth, inside-or-around the gesture. Bearer from URL/localStorage; the
  //    passkey terminal token from the live store, or the passkey ceremony.
  const urlTok = tokenFromUrl();
  if (urlTok) writeToken(urlTok);
  opts.token = readToken();
  try {
    state.webauthn = await jsonApi('/api/webauthn/status');
  } catch (_) { /* loopback / unconfigured — terminal token not needed */ }
  try {
    opts.terminalToken = await ensureTerminalToken();
  } catch (exc) {
    opts.terminalToken = readTerminalToken();
    log('⚠️ passkey unlock skipped: ' + (exc.message || exc), 'warn');
  }

  // 2) Probe the hub TTS voice; fall back to Web Speech if unreachable.
  try { await probeHub(opts); } catch (_) { /* probe caches false */ }
  useHub = isHubAvailable();
  log(useHub ? '🔊 narration via hub Orpheus TTS' : '🔊 narration via Web Speech (hub unavailable)');

  // 3) One mic permission grant + one long-lived stream + one analyser, reused
  //    every turn — re-using the granted stream is the whole no-tap bet.
  try {
    micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (exc) {
    log('❌ microphone unavailable: ' + (exc.message || exc), 'err');
    $('startBtn').disabled = false;
    return;
  }
  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  audioCtx = new AudioCtx();
  try { await audioCtx.resume(); } catch (_) { /* best effort */ }
  const src = audioCtx.createMediaStreamSource(micStream);
  analyser = audioCtx.createAnalyser();
  analyser.fftSize = 1024;
  vadData = new Float32Array(analyser.fftSize);
  src.connect(analyser);
  micStream.getAudioTracks().forEach((tr) => {
    tr.addEventListener('mute', () => log('🔇 mic track muted — ' + snapshot(), 'warn'));
    tr.addEventListener('ended', () => log('🛑 mic track ended — ' + snapshot(), 'err'));
  });

  // 4) Narration-end → turn-end → re-arm, registered once.
  onSpeechEnd(() => {
    if (loop.state === STATE.NARRATING) {
      log('✅ narration finished — turn complete', 'ok');
      apply(loop.onNarrationEnd());
    }
  });
  onSpeakingChange((on) => { if (!on) renderMetrics(); });

  running = true;
  $('stopBtn').hidden = false;
  vadTimer = setInterval(vadTick, VAD_POLL_MS);
  apply(loop.begin());
}

function teardown() {
  if (vadTimer) { clearInterval(vadTimer); vadTimer = null; }
  try { if (recorder && recorder.state !== 'inactive') recorder.stop(); } catch (_) { /* */ }
  recorder = null;
  stopNarration();
  if (micStream) { micStream.getTracks().forEach((tr) => tr.stop()); micStream = null; }
  if (audioCtx) { try { audioCtx.close(); } catch (_) { /* */ } audioCtx = null; }
  analyser = null;
  running = false;
}

function stop() {
  log('⏹️ stop tapped');
  apply(loop.halt());
  $('startBtn').disabled = false;
  $('stopBtn').hidden = true;
  showResume(false);
  renderMetrics();
}

function init() {
  $('startBtn').addEventListener('click', start);
  $('stopBtn').addEventListener('click', stop);
  $('resumeBtn').addEventListener('click', onResume);
  // Close: release the mic + audio and go back to the launcher (or the prior
  // page if we were navigated here from the footer link).
  $('closeBtn').addEventListener('click', () => {
    teardown();
    if (window.history.length > 1) window.history.back();
    else window.location.href = '/';
  });
  wireLifecycle();
  renderMetrics();
  log('Ready. Tap Start, then speak — the loop should run hands-free.');
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
