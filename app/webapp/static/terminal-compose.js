/* Compose bar for the live terminal (issue #315 split off terminal.js):
 * the predictive-text textarea, voice dictation, and screenshot-OCR staging
 * tray that feed it — everything behind the ➤ Send / 🎤 / 📷 button row.
 *
 * Compose bar (issue #37): a normal <textarea> with default predictive/
 * autocorrect/spellcheck so iOS/Android keyboards offer suggestions —
 * which they can't inside xterm's per-keystroke-wiped helper textarea.
 * ➤ Send forwards the buffered text, then a submitting \r as a SEPARATE
 * WS frame (see sendSubmit / #166).
 *
 * Voice dictation (issues #165 / #168): the 🎤 button records the mic and
 * drops the transcript into the compose textarea for review — never
 * straight into the PTY. The recording pipeline itself (streamed partials
 * with the single-shot fallback) lives in the shared voice.js since #302 —
 * this is just the compose bar's mounted instance. ➤ Send refuses to run
 * while composeDictation.isBusy() (#489) — a stopped-but-still-finalizing
 * dictation is still writing into the tracked span; Send racing that window
 * cleared the buffer under it instead of ever seeing the settled transcript.
 *
 * Screenshot OCR (issue #171): the 📷 button *stages* one or more
 * screenshots into a tray; nothing is sent yet. Each tap accumulates more
 * images. When the user taps Extract, ALL staged images go to photo-ocr in
 * a SINGLE /api/ocr call, so photo-ocr collates them into one deduplicated
 * text (overlapping shots of one document are merged, duplicate boundary
 * lines removed) — instead of one isolated OCR per image. The text drops
 * into the compose textarea for review before ➤ Send.
 */

import { els, state } from './state.js';
import { apiFailToast, apiRaw, toast } from './api.js';
import { readTerminalToken } from './webauthn.js';
import { createDictation, startWorkTimer } from './voice.js';
import { icon } from './_vendored/icons/icons.js';
import { stopReading } from './terminal-readaloud.js';

// Max visible rows before the textarea scrolls internally. Roomy enough
// for a long dictated voice note (#165) without the bar eating the whole
// screen when the keyboard is up. The CSS min-height floors it at 2 rows.
const _COMPOSE_MAX_ROWS = 8;

// Delay (ms) before the submitting CR when the compose payload carries a
// pasted image path (issue #450). Gives Claude Code's path→attachment
// conversion time to finish so the CR isn't absorbed by it. On-device tunable:
// too small and the first tap still needs a second Enter; larger only adds a
// little submit latency on image sends, so this errs generous.
const _IMAGE_SUBMIT_DELAY_MS = 350;

export function growComposeInput() {
  // Auto-grow up to _COMPOSE_MAX_ROWS; the iOS return key adds newlines,
  // only ➤ Send forwards to the PTY.
  const ta = els.terminalComposeInput;
  ta.style.height = 'auto';
  const lineHeight = parseFloat(getComputedStyle(ta).lineHeight) || 20;
  ta.style.height =
    Math.min(ta.scrollHeight, _COMPOSE_MAX_ROWS * lineHeight + 16) + 'px';
  // Keep the caret (end of a freshly inserted transcript) in view.
  ta.scrollTop = ta.scrollHeight;
}

export function resetComposeBar() {
  els.terminalComposeBar.hidden = true;
  els.terminalComposeInput.value = '';
  els.terminalComposeInput.style.height = '';
  // #450: an emptied buffer no longer carries an attached image path.
  if (state.terminal) state.terminal.composeHasImage = false;
  clearOcrStaging();
}

function setComposeOpen(open) {
  const t = state.terminal;
  if (!t) return;
  t.composeOpen = open;
  els.terminalComposeBar.hidden = !open;
  if (!open) {
    // Closing the bar abandons any in-flight recording so the mic isn't
    // left live behind a hidden bar, and drops any staged OCR images.
    composeDictation.stop();
    clearOcrStaging();
  }
  if (open) {
    // Focusing the textarea pops the phone keyboard with predictive on.
    els.terminalComposeInput.focus();
  } else if (t.term) {
    // Direct mode resumes — hand focus back to xterm.
    t.term.focus();
  }
}

// Wrap a clipboard / compose payload in bracketed-paste markers (DECSET
// 2004) when the agent's TUI has them enabled, so it buffers the whole
// block as one atomic paste instead of absorbing a per-keystroke burst —
// which the Windows console input queue silently drops spans of under a
// multi-KB load (#64). This is exactly what xterm already does for its own
// native paste (term.onData); the 📋 button and compose ➤ Send bypass
// xterm, so they have to replicate it. Only bracket when the app actually
// asked for it (`term.modes.bracketedPasteMode`) — otherwise the literal
// `\x1b[200~` would land as garbage in an agent that doesn't grok it.
//
// Framing only — this never appends the submitting carriage return. A
// submit goes through `sendSubmit`, which delivers the CR as its OWN WS
// frame after this block (see #166).
export function framePaste(t, text) {
  const bracketed = !!(t.term && t.term.modes && t.term.modes.bracketedPasteMode);
  if (!bracketed) return text;
  return '\x1b[200~' + text + '\x1b[201~';
}

// Send a composed prompt to the PTY and submit it. The submitting carriage
// return is sent as its OWN WS frame *after* the (possibly bracketed) text
// block — never concatenated onto it.
//
// Why split: the webapp proxies each WS `input` frame to the session-host
// as a distinct `pty.write()`, so two frames become two PTY writes. That
// guarantees the `\x1b[201~` paste-end marker is written — and the TUI has
// finished exiting bracketed-paste mode — before the bare CR arrives. When
// the CR rode in the same frame as the end marker, the TUI intermittently
// absorbed it into paste finalization instead of running the prompt: the
// "➤ Send sometimes does nothing" race of #166. A CR *inside* the markers
// is literal pasted text by design, so the split is the only ordering that
// reliably submits. With bracketed mode off there is no paste state machine
// to race, but the two-frame path is harmless there, so it stays uniform.
// `opts.submitDelayMs` (issue #450): hold the submitting CR back by this many
// ms instead of sending it in the same burst as the text. Needed only when the
// payload carries a pasted image path — Claude Code runs a path→attachment
// conversion on submit that absorbs a CR arriving mid-conversion, so the prompt
// lands unsubmitted and needs a second Enter. A short defer lets the conversion
// settle before the CR arrives. Plain-text sends pass no delay and stay
// instant (the CR still goes as its own frame, preserving #166's ordering fix).
export function sendSubmit(t, text, opts) {
  if (!t || !t.ws || t.ws.readyState !== WebSocket.OPEN) return;
  t.ws.send(JSON.stringify({ type: 'input', data: framePaste(t, text) }));
  const submit = function () {
    if (t.ws && t.ws.readyState === WebSocket.OPEN) {
      t.ws.send(JSON.stringify({ type: 'input', data: '\r' }));
    }
  };
  const delay = opts && opts.submitDelayMs;
  if (delay > 0) setTimeout(submit, delay); else submit();
}

// Voice dictation instance mounted on the compose bar. Starting to talk
// silences any in-flight read-aloud (issue #190): you're answering, not
// still listening.
const composeDictation = createDictation({
  button: els.terminalRecord,
  getTextarea: function () { return els.terminalComposeInput; },
  onRender: growComposeInput,
  onStart: stopReading,
});

let _ocrStaged = [];        // File objects awaiting a collated extraction
let _ocrThumbUrls = [];     // object URLs to revoke when the tray clears

function clearOcrStaging() {
  _ocrStaged = [];
  _ocrThumbUrls.forEach(function (u) { URL.revokeObjectURL(u); });
  _ocrThumbUrls = [];
  renderOcrTray();
}

function renderOcrTray() {
  const strip = els.terminalOcrThumbs;
  strip.innerHTML = '';
  _ocrThumbUrls.forEach(function (u) { URL.revokeObjectURL(u); });
  _ocrThumbUrls = [];
  if (!_ocrStaged.length) {
    els.terminalOcrTray.hidden = true;
    return;
  }
  els.terminalOcrTray.hidden = false;
  _ocrStaged.forEach(function (file, idx) {
    const cell = document.createElement('div');
    cell.className = 'ocr-thumb';
    const img = document.createElement('img');
    const url = URL.createObjectURL(file);
    _ocrThumbUrls.push(url);
    img.src = url;
    img.alt = 'staged screenshot ' + (idx + 1);
    const rm = document.createElement('button');
    rm.type = 'button';
    rm.className = 'ocr-thumb-x';
    rm.innerHTML = icon('x');
    rm.title = 'Remove';
    rm.addEventListener('click', function () {
      _ocrStaged.splice(idx, 1);
      renderOcrTray();
    });
    cell.appendChild(img);
    cell.appendChild(rm);
    strip.appendChild(cell);
  });
  els.terminalOcrExtract.innerHTML =
    icon('camera') + ' Extract text (' + _ocrStaged.length + ')';
}

function stageOcrImages(files) {
  const list = files ? Array.prototype.slice.call(files) : [];
  if (!list.length) return;
  _ocrStaged = _ocrStaged.concat(list);
  renderOcrTray();
}

// Run OCR over EVERY staged image in one call so photo-ocr deduplicates the
// overlap. Headers mirror sendImage in terminal.js (bearer + passkey terminal
// token).
async function runOcrExtraction() {
  const list = _ocrStaged.slice();
  if (!list.length) return;
  const fd = new FormData();
  list.forEach(function (f, i) {
    fd.append('files', f, f.name || ('screenshot-' + (i + 1) + '.png'));
  });
  const btn = els.terminalOcrExtract;
  btn.disabled = true;
  els.terminalScreenshot.disabled = true;
  const stopTimer = startWorkTimer(btn, icon('camera') + ' Extract text', icon('hourglass') + ' Reading ');
  try {
    const tt = readTerminalToken();
    const res = await apiRaw('/api/ocr', {
      method: 'POST', terminalToken: tt, body: fd,
    });
    if (!res.ok) {
      const b = await res.json().catch(function () { return null; });
      throw new Error((b && b.detail) || ('HTTP ' + res.status));
    }
    const body = await res.json().catch(function () { return null; });
    const text = body && body.text;
    const plural = list.length > 1;
    if (!text) {
      toast('📷 No text found in the image' + (plural ? 's' : ''));
      return;
    }
    // Insert at the caret with a leading space when the textarea already
    // has trailing content, so the OCR appends cleanly to typed text.
    const ta = els.terminalComposeInput;
    const before = ta.value.slice(0, ta.selectionStart);
    const sep = (before && !/\s$/.test(before)) ? ' ' : '';
    ta.setRangeText(sep + text, ta.selectionStart, ta.selectionEnd, 'end');
    growComposeInput();
    ta.focus();
    clearOcrStaging();
    toast(
      '📷 Text extracted from ' + list.length + ' image' +
        (plural ? 's' : '') + ' — review, then ➤ Send.',
      'good'
    );
  } catch (exc) {
    apiFailToast('OCR failed', exc);
  } finally {
    stopTimer();
    btn.disabled = false;
    els.terminalScreenshot.disabled = false;
  }
}

export function wireCompose() {
  els.terminalCompose.addEventListener('click', function () {
    const t = state.terminal;
    if (!t) return;
    setComposeOpen(!t.composeOpen);
  });
  els.terminalRecord.addEventListener('click', composeDictation.toggle);
  els.terminalScreenshot.addEventListener('click', function () {
    els.terminalScreenshotInput.click();
  });
  // General attach (issue #366): second entry point into the existing
  // sendImage()/#terminalImageInput flow (terminal.js owns the change
  // handler) — reachable from the compose-bar view, unlike the outer-bar
  // 🖼 button. No accept filter, so iOS offers Files as well as Photos.
  if (els.terminalComposeAttach) {
    els.terminalComposeAttach.addEventListener('click', function () {
      els.terminalImageInput.click();
    });
  }
  els.terminalScreenshotInput.addEventListener('change', function () {
    const picked = els.terminalScreenshotInput.files;
    const list = picked && picked.length
      ? Array.prototype.slice.call(picked) : [];
    els.terminalScreenshotInput.value = '';
    // Stage, don't send — accumulate across taps; Extract collates them all.
    if (list.length) stageOcrImages(list);
  });
  els.terminalOcrExtract.addEventListener('click', runOcrExtraction);
  els.terminalComposeSend.addEventListener('click', function () {
    const t = state.terminal;
    if (!t || !t.ws || t.ws.readyState !== WebSocket.OPEN) return;
    // #489: a dictation that just stopped is still finalizing (POST
    // /finish) until composeDictation settles the canonical transcript into
    // the textarea. Reading+clearing the buffer mid-window raced that
    // settle — the longer the recording, the wider the window. Wait it out
    // instead of racing it; the transcript lands a beat later and Send works
    // normally then.
    if (composeDictation.isBusy()) {
      toast('🎤 Still transcribing — wait for the transcript, then ➤ Send', 'error');
      return;
    }
    const text = els.terminalComposeInput.value;
    if (!text) return;
    // #450: when an image path was attached into this buffer, defer the
    // submitting CR so Claude Code's attachment conversion doesn't swallow it.
    const opts = t.composeHasImage
      ? { submitDelayMs: _IMAGE_SUBMIT_DELAY_MS } : undefined;
    sendSubmit(t, text, opts);
    t.composeHasImage = false;
    els.terminalComposeInput.value = '';
    els.terminalComposeInput.style.height = '';
    els.terminalComposeInput.focus();
  });
  els.terminalComposeInput.addEventListener('input', growComposeInput);
}
