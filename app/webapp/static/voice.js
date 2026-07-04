/* Shared voice dictation (issue #302 — extracted verbatim from terminal.js).
 *
 * One module, many mics: the compose bar (issues #165 / #168), the Board
 * dispatch bar and the Board drawer reply box (#302) each mount a
 * `createDictation` instance on their own button + textarea. The recording
 * pipeline is unchanged from the compose-bar original: preferred flow is
 * *streamed* (create a voice session, POST audio chunks at a 1 s cadence,
 * subscribe to an SSE stream of rolling `partial` transcripts that revise
 * the dictated span live); on any streaming-setup failure it falls back to
 * the single-shot path (buffer the whole take, POST once to
 * /api/transcribe) so dictation degrades rather than breaks. The `finish`
 * call is the source of truth either way. The transcript always lands in
 * the target textarea for review — never straight into a PTY or a dispatch.
 */

import { apiFailToast, authHeaders, readToken, toast } from './api.js';
import { readTerminalToken } from './webauthn.js';

const _CHUNK_MS = 1000;

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

// Auth headers for the transcribe endpoints (bearer + passkey terminal
// token), mirroring sendImage.
export function voiceHeaders() {
  return authHeaders({ terminalToken: readTerminalToken() });
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
// label for wide buttons. Returns a stop() that restores ``restoreText``.
export function startWorkTimer(btn, restoreText, workingLabel) {
  const lbl = workingLabel || '⏳';
  const t0 = Date.now();
  btn.classList.add('working');
  function tick() {
    const s = Math.floor((Date.now() - t0) / 1000);
    btn.textContent = lbl + s + 's';
  }
  tick();
  const id = setInterval(tick, 500);
  return function stop() {
    clearInterval(id);
    btn.classList.remove('working');
    btn.textContent = restoreText;
  };
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
 *                  bar silences an in-flight read-aloud, #190),
 *   }) → { toggle, stop, isRecording }
 */
export function createDictation(opts) {
  const button = opts.button;
  const getTextarea = opts.getTextarea;
  const onRender = opts.onRender || function () {};
  const onStart = opts.onStart || function () {};

  let _recorder = null;
  let _recordChunks = [];
  // Streaming state (#168). _voiceSession is the upstream session id;
  // _streaming flips true only once a session is created. The chunk queue
  // drains sequentially so chunks reach the session-host in order.
  let _voiceSession = null;
  let _streaming = false;
  let _voiceEvents = null;
  let _chunkQueue = [];
  let _chunkDraining = false;
  // The dictated span inside the textarea: [_dictStart, _dictStart+_dictLen].
  // Each partial replaces exactly that span, preserving text typed before it.
  let _dictStart = 0;
  let _dictLen = 0;

  function setRecordingUI(on) {
    if (!button) return;
    button.classList.toggle('recording', on);
    button.setAttribute('aria-pressed', on ? 'true' : 'false');
    button.textContent = on ? '⏹' : '🎤';
    button.title = on ? 'Stop recording' : 'Dictate (voice → text)';
  }

  // Replace the tracked dictation span with the latest transcript, leaving
  // any text the user typed before the span untouched, and keep the caret /
  // end in view.
  function renderDictation(text) {
    const ta = getTextarea();
    ta.setRangeText(text, _dictStart, _dictStart + _dictLen, 'end');
    _dictLen = text.length;
    onRender();
  }

  function closeVoiceEvents() {
    if (_voiceEvents) {
      try { _voiceEvents.close(); } catch (_) { /* best effort */ }
      _voiceEvents = null;
    }
  }

  // Sequentially POST queued audio chunks so they reach the session-host in
  // order (overlapping POSTs could interleave on the raw file).
  async function drainChunks() {
    if (_chunkDraining) return;
    _chunkDraining = true;
    try {
      while (_chunkQueue.length && _voiceSession) {
        const blob = _chunkQueue.shift();
        try {
          await fetch(
            '/api/transcribe/sessions/' + encodeURIComponent(_voiceSession) +
              '/chunk',
            { method: 'POST', headers: voiceHeaders(), body: blob }
          );
        } catch (_) { /* a dropped chunk is recoverable; finish reconciles */ }
      }
    } finally {
      _chunkDraining = false;
    }
  }

  async function startRecording() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia ||
        !window.MediaRecorder) {
      toast('Recording not supported on this browser', 'error');
      return;
    }
    if (_activeInstance && _activeInstance !== api) {
      toast('Another dictation is already recording', 'error');
      return;
    }
    onStart();
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (exc) {
      apiFailToast('Microphone unavailable', exc);
      return;
    }
    const mime = pickAudioMime();
    try {
      _recorder = mime
        ? new MediaRecorder(stream, { mimeType: mime })
        : new MediaRecorder(stream);
    } catch (exc) {
      stream.getTracks().forEach(function (tr) { tr.stop(); });
      apiFailToast('Recorder failed', exc);
      return;
    }
    _activeInstance = api;
    _recordChunks = [];
    _chunkQueue = [];
    _voiceSession = null;
    _streaming = false;

    // Try to open a streamed session (#168). On any failure, fall back to
    // the buffered single-shot path (#165) — _streaming stays false.
    try {
      const res = await fetch('/api/transcribe/sessions', {
        method: 'POST', headers: voiceHeaders(),
      });
      if (res.ok) {
        const body = await res.json().catch(function () { return null; });
        if (body && body.session_id) {
          _voiceSession = body.session_id;
          _streaming = true;
        }
      }
    } catch (_) { /* fall back to buffered */ }

    if (_streaming) {
      // Anchor the dictation span at the caret (after a separator space when
      // the textarea already has trailing content), then stream partials in.
      const ta = getTextarea();
      const before = ta.value.slice(0, ta.selectionStart);
      const sep = (before && !/\s$/.test(before)) ? ' ' : '';
      ta.setRangeText(sep, ta.selectionStart, ta.selectionEnd, 'end');
      _dictStart = ta.selectionStart;
      _dictLen = 0;
      try {
        _voiceEvents = new EventSource(
          '/api/transcribe/sessions/' + encodeURIComponent(_voiceSession) +
            '/events' + voiceQuery()
        );
        _voiceEvents.addEventListener('partial', function (ev) {
          try {
            const data = JSON.parse(ev.data);
            if (typeof data.transcript === 'string') renderDictation(data.transcript);
          } catch (_) { /* ignore a malformed frame */ }
        });
        // `final` also arrives via /finish's return value; closing here is
        // harmless — finish() is the source of truth.
        _voiceEvents.addEventListener('final', closeVoiceEvents);
      } catch (_) { _voiceEvents = null; }
    }

    _recorder.addEventListener('dataavailable', function (ev) {
      if (!ev.data || !ev.data.size) return;
      if (_streaming) {
        _chunkQueue.push(ev.data);
        drainChunks();
      } else {
        _recordChunks.push(ev.data);
      }
    });
    _recorder.addEventListener('stop', function () {
      stream.getTracks().forEach(function (tr) { tr.stop(); });
      setRecordingUI(false);
      _activeInstance = null;
      if (_streaming) {
        finishStreaming();
      } else {
        const type = _recorder ? _recorder.mimeType : (mime || 'audio/webm');
        const blob = new Blob(_recordChunks, { type: type });
        _recordChunks = [];
        _recorder = null;
        if (blob.size) sendBufferedRecording(blob);
      }
    });
    // Timeslice only matters when streaming — it forces periodic
    // dataavailable so chunks flow during the take.
    _recorder.start(_streaming ? _CHUNK_MS : undefined);
    setRecordingUI(true);
  }

  function stopRecording() {
    if (_recorder && _recorder.state !== 'inactive') {
      try { _recorder.stop(); } catch (_) { /* stop fires anyway */ }
    } else {
      setRecordingUI(false);
    }
  }

  // Streamed stop (#168): flush remaining chunks, ask the voice-transcriber
  // for the canonical transcript, settle the dictated span, tear down.
  async function finishStreaming() {
    const sid = _voiceSession;
    _recorder = null;
    button.disabled = true;
    const stopTimer = startWorkTimer(button, '🎤');
    try {
      await drainChunks();
      const res = await fetch(
        '/api/transcribe/sessions/' + encodeURIComponent(sid) + '/finish',
        { method: 'POST', headers: voiceHeaders() }
      );
      if (!res.ok) {
        const b = await res.json().catch(function () { return null; });
        throw new Error((b && b.detail) || ('HTTP ' + res.status));
      }
      const body = await res.json().catch(function () { return null; });
      if (body && body.silent) {
        // Nothing heard — drop the empty span we anchored.
        renderDictation('');
        toast('🎤 Nothing heard — silent recording');
      } else if (body && typeof body.transcript === 'string') {
        renderDictation(body.transcript);
        toast('🎤 Transcribed — review, then ➤ Send.', 'good');
      }
      getTextarea().focus();
    } catch (exc) {
      apiFailToast('Transcription failed', exc);
    } finally {
      closeVoiceEvents();
      _voiceSession = null;
      _streaming = false;
      stopTimer();
      button.disabled = false;
    }
  }

  // Single-shot fallback (#165): the whole take in one POST to /api/transcribe.
  async function sendBufferedRecording(blob) {
    const ext = (blob.type && blob.type.indexOf('mp4') >= 0) ? 'mp4' : 'webm';
    const fd = new FormData();
    fd.append('file', blob, 'recording.' + ext);
    button.disabled = true;
    const stopTimer = startWorkTimer(button, '🎤');
    try {
      const res = await fetch('/api/transcribe', {
        method: 'POST', headers: voiceHeaders(), body: fd,
      });
      if (!res.ok) {
        const b = await res.json().catch(function () { return null; });
        throw new Error((b && b.detail) || ('HTTP ' + res.status));
      }
      const body = await res.json().catch(function () { return null; });
      const text = body && body.transcript;
      if (body && body.silent) {
        toast('🎤 Nothing heard — silent recording');
        return;
      }
      if (!text) {
        toast('🎤 No transcript returned');
        return;
      }
      // Insert at the caret with a leading space when the textarea already
      // has trailing content, so dictation appends cleanly to typed text.
      const ta = getTextarea();
      const before = ta.value.slice(0, ta.selectionStart);
      const sep = (before && !/\s$/.test(before)) ? ' ' : '';
      ta.setRangeText(sep + text, ta.selectionStart, ta.selectionEnd, 'end');
      onRender();
      ta.focus();
      toast('🎤 Transcribed — review, then ➤ Send.', 'good');
    } catch (exc) {
      apiFailToast('Transcription failed', exc);
    } finally {
      stopTimer();
      button.disabled = false;
    }
  }

  const api = {
    toggle: function () {
      if (_recorder && _recorder.state === 'recording') stopRecording();
      else startRecording();
    },
    stop: stopRecording,
    isRecording: function () {
      return !!(_recorder && _recorder.state === 'recording');
    },
  };
  return api;
}
