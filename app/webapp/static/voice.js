/* Shared voice dictation (extracted verbatim from terminal.js).
 *
 * One module, many mics: the compose bar, the Board
 * dispatch bar and the shared composer (terminal, Chat, Board drawer) each mount a
 * `createDictation` instance on their own button + textarea. The recording
 * pipeline is unchanged from the compose-bar original: preferred flow is
 * *streamed* (create a voice session, POST audio chunks at a 1 s cadence,
 * subscribe to an SSE stream of rolling `partial` transcripts that revise
 * the dictated span live); on any streaming-setup failure it falls back to
 * the single-shot path (buffer the whole take, POST once to
 * /api/transcribe) so dictation degrades rather than breaks. The `finish`
 * call is the source of truth either way. The transcript always lands in
 * the target textarea for review — never straight into a PTY or a dispatch.
 *
 * A tap while recording stops and transcribes; a tap while the stopped take
 * is still transcribing cancels it: its requests are aborted, the
 * button is at rest again at once with "Dictation cancelled", and whatever
 * text is already in the textarea stays. The voice-transcriber has no abort
 * route, so its server-side session is simply dropped.
 */

import { AuthRequiredError, apiFailToast, apiRaw, apiRawJson, readToken, toast } from './api.js';
import { icon } from './_vendored/icons/icons.js';
import { state } from './state.js';
import { readTerminalToken } from './webauthn.js';

const _CHUNK_MS = 1000;
// A chunk upload that fails (network error or non-2xx) is retried this many
// times in all, backing off _CHUNK_RETRY_MS x attempt, before it is counted
// as lost. Chunks stay in order: the drain waits on each one.
const _CHUNK_ATTEMPTS = 3;
const _CHUNK_RETRY_MS = 500;

// The mic-button availability gate shared by every dictation mount point
// (compose bar, Board dispatch bar, Board drawer reply): the
// voice-transcriber must be configured server-side and the browser must
// support MediaRecorder.
export function voiceDictationAvailable() {
  return !!(state.status && state.status.voice_dictation) &&
    !!window.MediaRecorder;
}

// Only one mic can own the microphone at a time — starting a second
// instance while another records is refused, not silently hijacked.
let _activeInstance = null;

// First supported of the recorder MIME ladder — iOS Safari usually only
// offers audio/mp4, everyone else audio/webm/opus. The voice-transcriber
// sniffs the real container at transcode time, so a truthful label is all
// that matters.
export function pickAudioMime() {
  const candidates = [
    'audio/webm;codecs=opus',
    'audio/webm',
    'audio/mp4;codecs=mp4a.40.2',
    'audio/mp4',
  ];
  const MR = window.MediaRecorder;
  if (!MR || !MR.isTypeSupported) return '';
  for (let i = 0; i < candidates.length; i++) {
    if (MR.isTypeSupported(candidates[i])) return candidates[i];
  }
  return '';
}

// EventSource can't set headers, so the SSE stream carries auth in the
// query string (the gates read query params).
function voiceQuery() {
  const params = new URLSearchParams();
  const bt = readToken();
  if (bt) params.set('token', bt);
  const tt = readTerminalToken();
  if (tt) params.set('tt', tt);
  const q = params.toString();
  return q ? '?' + q : '';
}

// Tiny "working" indicator: swap a button's label for a ticking
// elapsed-seconds timer so a blind background wait — OCR, single-shot
// transcribe, streamed finish — visibly shows progress instead of looking
// stuck. ``workingLabel`` defaults to the hourglass glyph; pass a richer
// label for wide buttons. Labels are HTML (the Lucide icon() markup rides
// them). Returns a stop() that restores ``restoreHtml``.
export function startWorkTimer(btn, restoreHtml, workingLabel) {
  const lbl = workingLabel || icon('hourglass') + ' ';
  const t0 = Date.now();
  btn.classList.add('working');
  function tick() {
    const s = Math.floor((Date.now() - t0) / 1000);
    btn.innerHTML = lbl + s + 's';
  }
  tick();
  const id = setInterval(tick, 500);
  return function stop() {
    clearInterval(id);
    btn.classList.remove('working');
    btn.innerHTML = restoreHtml;
  };
}

// One mic's mutable state, threaded through the module-level helpers below.
// The helpers read and write these fields instead of closing over locals, so
// each stays a small function of its own.
function newDictationState(opts) {
  return {
    button: opts.button,
    getTextarea: opts.getTextarea,
    onRender: opts.onRender || function () {},
    onStart: opts.onStart || function () {},
    // The public object createDictation returns; the one-mic-at-a-time mutex
    // compares against it.
    api: null,
    recorder: null,
    recordChunks: [],
    // The live getUserMedia stream, held from the moment it's granted until
    // the recorder's own 'stop' handler releases its tracks. Lets dispose()
    // release the mic even in the narrow window after permission is
    // granted but before the recorder has actually started.
    stream: null,
    // Set by dispose(): startRecording()'s two async gaps (the
    // getUserMedia permission prompt, the streamed-session POST) check this
    // after resuming and bail out releasing the mic instead of proceeding;
    // the 'stop' handler checks it to skip finish/transcribe entirely for a
    // caller that no longer wants this take. Reset false at the top of every
    // startRecording() call, so a disposed-while-idle instance can record
    // again normally.
    aborted: false,
    // Streaming state. voiceSession is the upstream session id;
    // streaming flips true only once a session is created. The chunk queue
    // drains sequentially so chunks reach the session-host in order.
    voiceSession: null,
    streaming: false,
    voiceEvents: null,
    chunkQueue: [],
    // The drain in flight: one promise, settled only once the queue is
    // empty, so /finish can wait for the whole take. It used to be a boolean,
    // and a second caller — finishStreaming — returned at once while the
    // first drain still had chunks queued, so /finish closed the session
    // before the tail of the take was sent.
    drain: null,
    // Chunks that could not be delivered even after retries, this take.
    chunksLost: 0,
    // The dictated span inside the textarea: [dictStart, dictStart+dictLen].
    // Each partial replaces exactly that span, preserving text typed before it.
    dictStart: 0,
    dictLen: 0,
    // True from the moment recording stops until the canonical transcript has
    // settled. A long dictation gives the finalize network round
    // trip a real window to still be in flight when the user taps Send; Send
    // reading+clearing the textarea mid-window raced the late
    // renderDictation() call, which writes into the tracked span with
    // ``setRangeText`` — on an already-cleared textarea that clamps to offset
    // 0, so the final transcript landed unsent instead of Send ever seeing it.
    finishing: false,
    // True from the tap until the recorder is actually capturing:
    // the permission prompt and the streamed-session POST take a
    // moment, and a tap used to give no feedback, so the operator tapped
    // again — and that second tap started a second recorder, since the
    // mutex above only refuses *another* instance. While arming the button
    // shows a pending hourglass and toggle() ignores taps.
    arming: false,
    // The button's own accessible label and title, put back when a busy state
    // (arming, or finalizing a take) ends.
    labelHeld: false,
    idleLabel: null,
    idleTitle: '',
    // One take's requests: the session create, the chunk uploads, the
    // finish and the single-shot transcribe all carry this signal, so a cancel
    // or a dispose aborts whatever is still in flight. New per take.
    takeCtrl: null,
    // The finalizing phase in progress, or null: `{ stopTimer }`. A cancel ends
    // it at once and clears this, so the aborted request's own settle, when it
    // comes back later, sees it is no longer current and touches nothing.
    finishOp: null,
  };
}

function holdLabel(d, text) {
  const button = d.button;
  if (!button) return;
  if (!d.labelHeld) {
    d.labelHeld = true;
    d.idleLabel = button.getAttribute('aria-label');
    d.idleTitle = button.title;
  }
  button.setAttribute('aria-busy', 'true');
  button.setAttribute('aria-label', text);
  button.title = text;
}

function releaseLabel(d) {
  const button = d.button;
  if (!button || !d.labelHeld) return;
  d.labelHeld = false;
  button.removeAttribute('aria-busy');
  if (d.idleLabel === null) button.removeAttribute('aria-label');
  else button.setAttribute('aria-label', d.idleLabel);
  button.title = d.idleTitle;
  d.idleLabel = null;
}

// what a tap on the mic would do right now, as the usage log's
// `data-track` control id (usage.js). The log's click listener reads the
// attribute when the tap lands, before toggle() runs, so the id is kept in
// step with the state — each tap is then logged under what it did. A phone's
// console is out of reach; the server-side log is not. Constant ids only
// (the usage-log privacy contract): nothing about the take is recorded.
const _TAP_IDS = {
  start: 'mic.start',
  stop: 'mic.stop',
  arming: 'mic.ignored-arming',
  finalizing: 'mic.cancel-finalizing',
};

function tapState(d) {
  if (d.arming) return 'arming';
  if (d.finishOp) return 'finalizing';
  if (d.recorder && d.recorder.state === 'recording') return 'stop';
  return 'start';
}

function syncTrackId(d) {
  if (d.button) d.button.setAttribute('data-track', _TAP_IDS[tapState(d)]);
}

function setArmingUI(d) {
  const button = d.button;
  d.arming = true;
  syncTrackId(d);
  if (!button) return;
  holdLabel(d, 'Starting…');
  button.classList.add('arming');
  button.innerHTML = icon('hourglass');
}

function setRecordingUI(d, on) {
  const button = d.button;
  d.arming = false;
  syncTrackId(d);
  if (!button) return;
  if (button.classList.contains('arming')) {
    button.classList.remove('arming');
    releaseLabel(d);
  }
  button.classList.toggle('recording', on);
  button.setAttribute('aria-pressed', on ? 'true' : 'false');
  button.innerHTML = on ? icon('square') : icon('mic');
  button.title = on ? 'Stop recording' : 'Dictate (voice to text)';
}

// Replace the tracked dictation span with the latest transcript, leaving
// any text the user typed before the span untouched, and keep the caret /
// end in view.
function renderDictation(d, text) {
  const ta = d.getTextarea();
  ta.setRangeText(text, d.dictStart, d.dictStart + d.dictLen, 'end');
  d.dictLen = text.length;
  d.onRender();
}

function closeVoiceEvents(d) {
  if (d.voiceEvents) {
    try { d.voiceEvents.close(); } catch (_) { /* best effort */ }
    d.voiceEvents = null;
  }
}

// POST one chunk, retrying a failure. A non-2xx answer is a failure too:
// api() throws only on 401, so a 502 from a loaded proxy used to count as
// delivered. Resolves true once the server accepted the chunk.
async function sendChunk(sid, blob, signal) {
  for (let attempt = 1; attempt <= _CHUNK_ATTEMPTS; attempt++) {
    if (signal.aborted) return false;
    try {
      const res = await apiRaw(
        '/api/transcribe/sessions/' + encodeURIComponent(sid) + '/chunk',
        { method: 'POST', terminalToken: readTerminalToken(), body: blob, signal: signal }
      );
      if (res.ok) return true;
      console.warn('dictation: chunk upload answered HTTP ' + res.status +
        ' (attempt ' + attempt + '/' + _CHUNK_ATTEMPTS + ')');
    } catch (exc) {
      if (exc instanceof AuthRequiredError || signal.aborted) return false;
      console.warn('dictation: chunk upload failed (attempt ' + attempt +
        '/' + _CHUNK_ATTEMPTS + ')', exc);
    }
    if (attempt < _CHUNK_ATTEMPTS) {
      await new Promise(function (r) { setTimeout(r, _CHUNK_RETRY_MS * attempt); });
    }
  }
  return false;
}

// Sequentially POST queued audio chunks so they reach the session-host in
// order (overlapping POSTs could interleave on the raw file). Every caller
// gets the same promise, which settles only when the queue is empty.
//
// Each POST carries everything queued so far, not one chunk: the
// recorder's chunks are consecutive slices of one byte stream, so joined
// they are still valid audio, in order. On 2026-09-25 a 1 s chunk took
// 3-5 s to upload under load; one chunk per request then fell further
// behind every second of speech, and at stop the rest of the take was
// still queued. Batched, a slow request simply carries more audio.
function drainChunks(d) {
  if (!d.drain) {
    const signal = d.takeCtrl.signal;
    d.drain = (async function () {
      try {
        while (d.chunkQueue.length && d.voiceSession && !signal.aborted) {
          const batch = d.chunkQueue.slice();
          const blob = batch.length === 1
            ? batch[0]
            : new Blob(batch, { type: batch[0].type });
          if (!(await sendChunk(d.voiceSession, blob, signal))) d.chunksLost += batch.length;
          d.chunkQueue.splice(0, batch.length);
        }
      } finally {
        // An aborted drain was already let go by dropFinishing(); clearing
        // d.drain now could drop the next take's.
        if (!signal.aborted) d.drain = null;
      }
    })();
  }
  return d.drain;
}

// The microphone stream, or null when the permission was refused or
// dispose() ran while the prompt was up (nothing else was claimed yet).
async function acquireStream(d) {
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (exc) {
    _activeInstance = null;
    setRecordingUI(d, false);
    apiFailToast('Microphone unavailable', exc);
    return null;
  }
  if (d.aborted) {
    // dispose() ran while the permission prompt was up — release
    // immediately, nothing else was ever claimed.
    stream.getTracks().forEach(function (tr) { tr.stop(); });
    _activeInstance = null;
    setRecordingUI(d, false);
    return null;
  }
  d.stream = stream;
  return stream;
}

// False when the recorder could not be built; the mic is released then.
function createRecorder(d, stream, mime) {
  try {
    d.recorder = mime
      ? new MediaRecorder(stream, { mimeType: mime })
      : new MediaRecorder(stream);
  } catch (exc) {
    _activeInstance = null;
    d.stream = null;
    stream.getTracks().forEach(function (tr) { tr.stop(); });
    setRecordingUI(d, false);
    apiFailToast('Recorder failed', exc);
    return false;
  }
  return true;
}

// Try to open a streamed session. On any failure, fall back to
// the buffered single-shot path — streaming stays false.
async function openStreamSession(d, signal) {
  try {
    const res = await apiRaw('/api/transcribe/sessions', {
      method: 'POST', terminalToken: readTerminalToken(), signal: signal,
    });
    if (res.ok) {
      const body = await res.json().catch(function () { return null; });
      if (body && body.session_id) {
        d.voiceSession = body.session_id;
        d.streaming = true;
      }
    }
  } catch (_) { /* fall back to buffered */ }
}

// Anchor the dictation span at the caret (after a separator space when
// the textarea already has trailing content), then stream partials in.
function startStreamedSpan(d) {
  const ta = d.getTextarea();
  const before = ta.value.slice(0, ta.selectionStart);
  const sep = (before && !/\s$/.test(before)) ? ' ' : '';
  ta.setRangeText(sep, ta.selectionStart, ta.selectionEnd, 'end');
  d.dictStart = ta.selectionStart;
  d.dictLen = 0;
  try {
    d.voiceEvents = new EventSource(
      '/api/transcribe/sessions/' + encodeURIComponent(d.voiceSession) +
        '/events' + voiceQuery()
    );
    d.voiceEvents.addEventListener('partial', function (ev) {
      try {
        const data = JSON.parse(ev.data);
        if (typeof data.transcript === 'string') renderDictation(d, data.transcript);
      } catch (_) { /* ignore a malformed frame */ }
    });
    // `final` also arrives via /finish's return value; closing here is
    // harmless — finish() is the source of truth.
    d.voiceEvents.addEventListener('final', function () { closeVoiceEvents(d); });
  } catch (_) { d.voiceEvents = null; }
}

function handleRecorderStop(d, stream, mime) {
  stream.getTracks().forEach(function (tr) { tr.stop(); });
  setRecordingUI(d, false);
  _activeInstance = null;
  d.stream = null;
  if (d.aborted) {
    // dispose() force-stopped an active recording — the mic is
    // already released above; skip finish/transcribe entirely, this
    // take is abandoned, not just paused.
    d.recordChunks = [];
    d.recorder = null;
    closeVoiceEvents(d);
    d.voiceSession = null;
    d.streaming = false;
    return;
  }
  if (d.streaming) {
    finishStreaming(d);
  } else {
    const type = d.recorder ? d.recorder.mimeType : (mime || 'audio/webm');
    const blob = new Blob(d.recordChunks, { type: type });
    d.recordChunks = [];
    d.recorder = null;
    if (blob.size) sendBufferedRecording(d, blob);
  }
}

async function startRecording(d) {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia ||
      !window.MediaRecorder) {
    toast('Recording not supported on this browser', 'error');
    return;
  }
  if (_activeInstance && _activeInstance !== d.api) {
    toast('Another dictation is already recording', 'error');
    return;
  }
  // Claim synchronously, before the first `await` — otherwise two taps
  // (e.g. dispatch-bar mic then a drawer reply-box mic) can both read
  // _activeInstance as falsy before either's getUserMedia() promise
  // settles, and both proceed. Released on every early-return
  // below; the 'stop' listener clears it on the normal path.
  _activeInstance = d.api;
  d.aborted = false;
  d.takeCtrl = new AbortController();
  const signal = d.takeCtrl.signal;
  setArmingUI(d);
  d.onStart();
  const stream = await acquireStream(d);
  if (!stream) return;
  const mime = pickAudioMime();
  if (!createRecorder(d, stream, mime)) return;
  d.recordChunks = [];
  d.chunkQueue = [];
  d.drain = null;
  d.chunksLost = 0;
  d.voiceSession = null;
  d.streaming = false;

  await openStreamSession(d, signal);

  if (d.aborted) {
    // dispose() ran while the session POST was in flight — the recorder
    // was constructed but never started, so no 'stop' event will ever
    // fire to release it; release here instead.
    stream.getTracks().forEach(function (tr) { tr.stop(); });
    _activeInstance = null;
    d.stream = null;
    d.recorder = null;
    closeVoiceEvents(d);
    d.voiceSession = null;
    d.streaming = false;
    setRecordingUI(d, false);
    return;
  }

  if (d.streaming) startStreamedSpan(d);

  d.recorder.addEventListener('dataavailable', function (ev) {
    if (!ev.data || !ev.data.size) return;
    if (d.streaming) {
      d.chunkQueue.push(ev.data);
      drainChunks(d);
    } else {
      d.recordChunks.push(ev.data);
    }
  });
  d.recorder.addEventListener('stop', function () { handleRecorderStop(d, stream, mime); });
  // Timeslice only matters when streaming — it forces periodic
  // dataavailable so chunks flow during the take.
  d.recorder.start(d.streaming ? _CHUNK_MS : undefined);
  setRecordingUI(d, true);
}

function stopRecording(d) {
  if (d.recorder && d.recorder.state !== 'inactive') {
    try { d.recorder.stop(); } catch (_) { /* stop fires anyway */ }
  } else {
    setRecordingUI(d, false);
  }
}

// The finalizing phase: from stop until the transcript has settled. The
// button stays tappable — a tap now cancels — so it is aria-busy
// with a Cancel label, never `disabled`: iOS fires no tap on a disabled
// button, which is how a hung request used to strand it for good.
function beginFinishing(d) {
  d.finishing = true;
  holdLabel(d, 'Cancel dictation');
  d.finishOp = { stopTimer: startWorkTimer(d.button, icon('mic')) };
  syncTrackId(d);
  return d.finishOp;
}

// Back to rest, once per phase. False when `op` was already ended (by a
// cancel or a dispose) — the caller's late settle then does nothing.
function endFinishing(d, op) {
  if (d.finishOp !== op) return false;
  d.finishOp = null;
  d.finishing = false;
  syncTrackId(d);
  op.stopTimer();
  releaseLabel(d);
  return true;
}

// Abandon a stopped take still finalizing: abort its requests, stop partials
// landing in the textarea and put the button at rest now, not whenever the
// aborted request comes back. Text already in the textarea stays. Returns
// whether a take was finalizing.
function dropFinishing(d) {
  const op = d.finishOp;
  if (!op) return false;
  if (d.takeCtrl) d.takeCtrl.abort();
  d.chunkQueue = [];
  d.drain = null;
  closeVoiceEvents(d);
  d.voiceSession = null;
  d.streaming = false;
  endFinishing(d, op);
  return true;
}

// Streamed stop: flush remaining chunks, ask the voice-transcriber
// for the canonical transcript, settle the dictated span, tear down.
async function finishStreaming(d) {
  const sid = d.voiceSession;
  const signal = d.takeCtrl.signal;
  d.recorder = null;
  const op = beginFinishing(d);
  // A dispose() or a cancel that lands mid-finalize: the
  // caller has moved on (a torn-down compose bar, a collapsed drawer, a
  // retry), so a late transcript must not be written into whatever textarea
  // getTextarea() now resolves to.
  const gone = function () { return d.aborted || d.finishOp !== op; };
  try {
    // The recorder's last dataavailable (the flush on stop) has just been
    // queued; wait until it and every chunk before it reached the server.
    await drainChunks(d);
    if (gone()) return;
    const body = await apiRawJson(
      '/api/transcribe/sessions/' + encodeURIComponent(sid) + '/finish',
      { method: 'POST', terminalToken: readTerminalToken(), signal: signal }
    );
    if (gone()) {
      // no-op
    } else if (body && body.silent) {
      // Nothing heard — drop the empty span we anchored.
      renderDictation(d, '');
      toast('Nothing heard — silent recording', undefined, { icon: 'mic' });
    } else if (body && typeof body.transcript === 'string') {
      renderDictation(d, body.transcript);
      if (d.chunksLost) {
        // Distinct from a failed transcription: the text is there, but
        // part of the take never reached the transcriber.
        toast('Transcribed, but ' + d.chunksLost + ' s of audio could not be ' +
          'uploaded — the text may be missing words.', 'error', { icon: 'mic' });
      } else {
        toast('Transcribed — review, then tap Send.', 'good', { icon: 'mic' });
      }
    }
    if (!gone()) d.getTextarea().focus();
  } catch (exc) {
    if (!gone()) apiFailToast('Transcription failed', exc);
  } finally {
    // A cancelled phase was torn down by dropFinishing(); a new take may
    // own the session fields by now.
    if (d.finishOp === op) {
      closeVoiceEvents(d);
      d.voiceSession = null;
      d.streaming = false;
      endFinishing(d, op);
    }
  }
}

// Single-shot fallback: the whole take in one POST to /api/transcribe.
async function sendBufferedRecording(d, blob) {
  const ext = (blob.type && blob.type.indexOf('mp4') >= 0) ? 'mp4' : 'webm';
  const fd = new FormData();
  fd.append('file', blob, 'recording.' + ext);
  const signal = d.takeCtrl.signal;
  const op = beginFinishing(d);
  // A dispose() or a cancel that lands mid-request must not
  // write a late transcript into whatever textarea getTextarea() now
  // resolves to.
  const gone = function () { return d.aborted || d.finishOp !== op; };
  try {
    const body = await apiRawJson('/api/transcribe', {
      method: 'POST', terminalToken: readTerminalToken(), body: fd, signal: signal,
    });
    if (gone()) return;
    const text = body && body.transcript;
    if (body && body.silent) {
      toast('Nothing heard — silent recording', undefined, { icon: 'mic' });
      return;
    }
    if (!text) {
      toast('No transcript returned', undefined, { icon: 'mic' });
      return;
    }
    // Insert at the caret with a leading space when the textarea already
    // has trailing content, so dictation appends cleanly to typed text.
    const ta = d.getTextarea();
    const before = ta.value.slice(0, ta.selectionStart);
    const sep = (before && !/\s$/.test(before)) ? ' ' : '';
    ta.setRangeText(sep + text, ta.selectionStart, ta.selectionEnd, 'end');
    d.onRender();
    ta.focus();
    toast('Transcribed — review, then tap Send.', 'good', { icon: 'mic' });
  } catch (exc) {
    if (!gone()) apiFailToast('Transcription failed', exc);
  } finally {
    endFinishing(d, op);
  }
}

// Force-stop + release the mic and abandon this take, for a caller that
// is tearing the mount point down rather than just closing the bar
// — e.g. leaving the terminal, or a Board drawer collapsing
// out from under a per-render dictation instance. Unlike `stop`, which
// still runs finish/transcribe to settle the result into the textarea,
// `dispose` guarantees the mic and the app-wide `_activeInstance` mutex
// are released even mid-permission-prompt or mid-finalize, and drops the
// in-flight result instead of writing it into a textarea nobody owns
// anymore. Safe to call when idle (no-op beyond flipping `aborted`,
// which the next `startRecording()` clears) and safe to call more than
// once.
function disposeDictation(d) {
  d.aborted = true;
  // Abort the take's requests too: nothing will read their answers.
  if (d.takeCtrl) d.takeCtrl.abort();
  if (d.recorder && d.recorder.state !== 'inactive') {
    // The 'stop' handler releases the stream, clears _activeInstance and
    // (seeing aborted) skips finish/transcribe.
    try { d.recorder.stop(); } catch (_) { /* stop fires anyway */ }
    return;
  }
  // Not actively recording: release whatever was already claimed —
  // an in-flight getUserMedia() that resolved but never reached
  // .start() (recorder construction or the session POST is still in
  // flight — those async gaps re-check aborted themselves), or an
  // already-stopped take still awaiting its finalize network round trip
  // (recorder/stream are already null there; only the flag matters).
  if (d.stream) {
    d.stream.getTracks().forEach(function (tr) { tr.stop(); });
    d.stream = null;
  }
  if (_activeInstance === d.api) _activeInstance = null;
  dropFinishing(d);
  closeVoiceEvents(d);
  setRecordingUI(d, false);
}

function toggleDictation(d) {
  // one info line per tap saying what it did. A report of "the mic
  // needed two taps" then reads off the device console: no line means the
  // tap never reached us (touch/layout), "ignored"/"cancelled" means the
  // state swallowed it, "start" means we did begin.
  if (d.arming) {
    console.info('[dictation] tap ignored: the mic is still arming');
    return;
  }
  // A tap while the stopped take is still transcribing cancels it.
  if (dropFinishing(d)) {
    console.info('[dictation] tap cancelled the take that was still finalizing');
    toast('Dictation cancelled', undefined, { icon: 'mic' });
    return;
  }
  if (d.recorder && d.recorder.state === 'recording') {
    console.info('[dictation] tap: stop recording');
    stopRecording(d);
  } else {
    console.info('[dictation] tap: start recording');
    startRecording(d);
  }
}

/* One dictation mic bound to one button + one target textarea.
 *
 *   createDictation({
 *     button:      the 🎤 <button> (UI state is handled here),
 *     getTextarea: () => the target <textarea> (a getter, because drawer
 *                  reply boxes are built per render),
 *     onRender:    optional — called after each transcript render (the
 *                  compose bar re-grows its textarea),
 *     onStart:     optional — called when recording starts (the compose
 *                  bar silences an in-flight read-aloud),
 *   }) → { toggle, stop, dispose, isRecording, isBusy }
 */
export function createDictation(opts) {
  const d = newDictationState(opts);
  syncTrackId(d);
  d.api = {
    toggle: function () { toggleDictation(d); },
    stop: function () { stopRecording(d); },
    dispose: function () { disposeDictation(d); },
    isRecording: function () {
      return !!(d.recorder && d.recorder.state === 'recording');
    },
    // True while actively recording OR while a stopped take is still
    // finalizing — the window a caller like Send must not race.
    isBusy: function () {
      return !!(d.recorder && d.recorder.state === 'recording') || d.finishing;
    },
  };
  return d.api;
}
