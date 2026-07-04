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
 * this is just the compose bar's mounted instance.
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
import { apiFailToast, authHeaders, toast } from './api.js';
import { readTerminalToken } from './webauthn.js';
import { createDictation, startWorkTimer } from './voice.js';
import { stopReading } from './terminal-readaloud.js';

// Max visible rows before the textarea scrolls internally. Roomy enough
// for a long dictated voice note (#165) without the bar eating the whole
// screen when the keyboard is up. The CSS min-height floors it at 2 rows.
const _COMPOSE_MAX_ROWS = 8;

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
export function sendSubmit(t, text) {
  if (!t || !t.ws || t.ws.readyState !== WebSocket.OPEN) return;
  t.ws.send(JSON.stringify({ type: 'input', data: framePaste(t, text) }));
  t.ws.send(JSON.stringify({ type: 'input', data: '\r' }));
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
    rm.textContent = '✕';
    rm.title = 'Remove';
    rm.addEventListener('click', function () {
      _ocrStaged.splice(idx, 1);
      renderOcrTray();
    });
    cell.appendChild(img);
    cell.appendChild(rm);
    strip.appendChild(cell);
  });
  els.terminalOcrExtract.textContent =
    '📷 Extract text (' + _ocrStaged.length + ')';
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
  const stopTimer = startWorkTimer(btn, '📷 Extract text', '⏳ Reading ');
  try {
    const tt = readTerminalToken();
    const res = await fetch('/api/ocr', {
      method: 'POST', headers: authHeaders({ terminalToken: tt }), body: fd,
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
    const text = els.terminalComposeInput.value;
    if (!text) return;
    sendSubmit(t, text);
    els.terminalComposeInput.value = '';
    els.terminalComposeInput.style.height = '';
    els.terminalComposeInput.focus();
  });
  els.terminalComposeInput.addEventListener('input', growComposeInput);
}
