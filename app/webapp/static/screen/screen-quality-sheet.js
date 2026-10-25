/* Screen view's quality sheet and readout overlay.
 *
 * Quality. A button in the tools row opens a small sheet with three
 * presets (Sharp, Balanced, Data saver) and a readout switch. The preset rides
 * the connect query (`preset=`) and is remembered per device, so a pick
 * reconnects through connect() like the first open; what each preset means is
 * the server's table. The readout is an overlay with fps, kB/s, the average
 * frame size and the time from ack to the next frame (screen-quality.js does
 * the arithmetic over the frames received), plus the PC's per-stage times from
 * the server's `stats` message. Switching it on sends one small `readout`
 * control message (not input, so it works in View too); with it off the server
 * sends no `stats` at all. The ack protocol is untouched.
 */

import {
  PRESETS, PRESET_KEY, Readout, presetChoice, presetNote, readoutLines,
} from './screen-quality.js';
import { $, view } from './screen-state.js';

// A preset pick reconnects through screen-view.js's connect(); wireQuality
// hands that in, so this module never imports the view back.
let onPresetChange = function () {};

// ------------------------------------------------------- quality

const READOUT_REFRESH_MS = 500;
let readoutTimer = null;

// The preset last picked on this device, or Balanced.
export function loadPreset() {
  let stored = null;
  try { stored = localStorage.getItem(PRESET_KEY); } catch (_) { /* storage blocked */ }
  view.preset = presetChoice(stored);
  renderQuality();
}

// A pick: remembered, then applied by reconnecting. The last frame stays
// under a quiet line until the new connection's first one is drawn.
function setPreset(id) {
  const next = presetChoice(id);
  if (next === view.preset) return;
  view.preset = next;
  try { localStorage.setItem(PRESET_KEY, next); } catch (_) { /* storage blocked */ }
  renderQuality();
  if (!view.open || view.monitorId === null) return;
  onPresetChange();
}

function renderQuality() {
  const group = $('screenPreset');
  if (group) {
    group.querySelectorAll('.segmented-item').forEach(function (btn) {
      btn.setAttribute('aria-pressed', btn.dataset.preset === view.preset ? 'true' : 'false');
    });
  }
  const note = $('screenPresetNote');
  if (note) note.textContent = presetNote(view.preset);
  const toggle = $('screenReadoutToggle');
  if (toggle) {
    toggle.setAttribute('aria-checked', view.readoutOn ? 'true' : 'false');
    const label = toggle.querySelector('.toggle-label');
    if (label) label.textContent = view.readoutOn ? 'ON' : 'OFF';
  }
}

// The one non-input message the client sends besides `ack`: it goes straight
// to the socket, not through sendInput, so View mode and a dead picture do not
// stop it, and the server does not count it as input.
export function sendReadoutSwitch(on) {
  const ws = view.ws;
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'readout', on: on }));
  }
}

export function setReadout(on) {
  if (on === view.readoutOn) return;
  view.readoutOn = on;
  view.serverStats = null;
  view.readout = on ? new Readout(performance.now()) : null;
  if (readoutTimer) clearInterval(readoutTimer);
  readoutTimer = on ? setInterval(renderReadout, READOUT_REFRESH_MS) : null;
  sendReadoutSwitch(on);
  renderQuality();
  renderReadout();
}

export function noteReadoutFrame(bytes) {
  if (view.readout) view.readout.frame(bytes, performance.now());
}

export function noteReadoutAck() {
  if (view.readout) view.readout.acked(performance.now());
}

export function onStats(msg) {
  if (view.readoutOn) view.serverStats = msg;
}

function renderReadout() {
  const el = $('screenReadout');
  if (!el) return;
  el.hidden = !view.readoutOn;
  if (!view.readoutOn || !view.readout) return;
  const lines = readoutLines(view.readout.snapshot(performance.now()), view.serverStats);
  el.replaceChildren();
  lines.forEach(function (line) {
    const row = document.createElement('div');
    row.textContent = line;
    el.appendChild(row);
  });
}

function openQualitySheet() {
  const sheet = $('screenQualitySheet');
  if (!sheet) return;
  renderQuality();
  if (!sheet.open) sheet.showModal();
}

export function closeQualitySheet() {
  const sheet = $('screenQualitySheet');
  if (sheet && sheet.open) sheet.close();
}

export function wireQuality(opts) {
  onPresetChange = opts.onPresetChange;
  const button = $('screenQuality');
  if (button) button.addEventListener('click', openQualitySheet);
  const group = $('screenPreset');
  if (group) {
    group.replaceChildren();
    PRESETS.forEach(function (preset) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'segmented-item';
      btn.textContent = preset.label;
      btn.dataset.preset = preset.id;
      btn.setAttribute('aria-pressed', 'false');
      btn.addEventListener('click', function () { setPreset(preset.id); });
      group.appendChild(btn);
    });
  }
  const toggle = $('screenReadoutToggle');
  if (toggle) toggle.addEventListener('click', function () { setReadout(!view.readoutOn); });
  const close = $('screenQualityClose');
  if (close) close.addEventListener('click', closeQualitySheet);
  const done = $('screenQualityDone');
  if (done) done.addEventListener('click', closeQualitySheet);
  const sheet = $('screenQualitySheet');
  if (sheet) {
    // A tap on the backdrop lands on the <dialog> itself, outside the card.
    sheet.addEventListener('click', function (e) {
      if (e.target === sheet) closeQualitySheet();
    });
  }
  renderQuality();
}
