/* Screen view: the PC's desktop, live, behind the terminal's gate.
 *
 * openScreen() is the one way in. On a phone it opens #screenView full
 * screen; on a desktop browser (fine pointer) it pops the same view out into
 * its own window (/?screen=1, which boots straight into it). Today the PC
 * load sheet's Screen row calls it; moving the entry to a header icon is a
 * one-line change at the caller.
 *
 * The socket (/api/screen/ws) sends JPEG frames as binary messages and JSON
 * status messages; the view acks each frame once it is drawn, and the server
 * grabs the next only then. The server sends nothing while the screen does
 * not change, so a still picture is a quiet socket, not a stall.
 *
 * Capture runs only while a viewer is connected, so the view closes its
 * socket when it closes and while the page is hidden, and reconnects when it
 * comes back. Pinch-zoom and pan are local (the page itself is zoom-locked):
 * two pointers zoom around their midpoint, one pans while zoomed, the wheel
 * zooms on a desktop.
 *
 * The rest of the dialog lives beside this file: screen-control.js (zoom,
 * the cursor, Control's gestures, the loupe), screen-keys.js (the keys bar,
 * a hardware keyboard, the clipboard keys) and screen-quality-sheet.js
 * (presets and the readout), all over the shared `view` in screen-state.js.
 *
 * Reconnect. A dropped socket is retried on its own, after 1 s, 2 s,
 * 4 s, 8 s and 15 s steps (screen-reconnect.js), with "Reconnecting…" over
 * the last frame; a drawn frame resets the count, and a spent budget falls
 * back to the Retry card. A refusal (4401, 4403, 4404) is never retried, and
 * neither is a hidden page or a closed view. Each attempt goes through
 * connect() like the first, so the passkey token, the Tailscale gate and the
 * tunnel refusal apply every time. The monitor picked is remembered per
 * device and reopened by its rectangle, then its id, then the primary.
 */

import { isDesktopClient, readToken, AuthRequiredError } from '../api.js';
import { clearTerminalToken, ensureTerminalToken, terminalJsonApi } from '../webauthn.js';
import { setAsyncState, staleMessage } from '../async-state.js';
import { controlStyle, wheelFromWheelEvent } from './screen-gestures.js';
import {
  MONITOR_KEY, chooseMonitor, monitorChoice, parseMonitorChoice, reconnectDelay,
} from './screen-reconnect.js';
import { $, view } from './screen-state.js';
import {
  applyZoom, cancelGesture, controlDown, controlMove, controlUp, dropGesture, framePoint, isPad, onCursor,
  padDown, padMove, padUp, placeCursor, placeLoupe, queueMove, resetZoom, syncPad, twoFingerState, wireControl,
  zoomAt,
} from './screen-control.js';
import {
  closePasteSheet, fitToKeyboard, onClipboard, resetClipboard, setKeysOpen, toggleKeys, wireHardwareKeyboard,
  wireKeyboard, wirePasteSheet,
} from './screen-keys.js';
import {
  closeQualitySheet, loadPreset, noteReadoutAck, noteReadoutFrame, onStats, sendReadoutSwitch, setReadout,
  wireQuality,
} from './screen-quality-sheet.js';

// The widest frame worth asking for: the phone's long side in device pixels,
// capped (the server never sends more than the monitor's own width).
const MAX_FRAME_WIDTH = 1920;
const POPUP_NAME = 'launcher-screen';
const POPUP_FEATURES = 'popup,width=1280,height=760';
// The last Control style used on this device.
const CONTROL_KEY = 'launcher.screenControlMode';

function stateHost() { return $('screenState'); }

function setState(name, opts) {
  setAsyncState(stateHost(), name, opts);
}

// ------------------------------------------------------------ entry point

export function openScreen() {
  if (isDesktopClient() && !view.standalone) {
    const popup = window.open('/?screen=1', POPUP_NAME, POPUP_FEATURES);
    if (popup) {
      popup.focus();
      return;
    }
    // A blocked pop-up falls through to the in-page view.
  }
  openScreenView();
}

// The pop-out window's boot path (main.js, ?screen=1).
export function openScreenWindow() {
  view.standalone = true;
  document.title = 'Screen';
  openScreenView();
}

async function openScreenView() {
  const dialog = $('screenView');
  if (!dialog) return;
  if (!dialog.open) dialog.showModal();
  view.open = true;
  loadPreset();
  setMode('view');
  resetZoom();
  await start();
}

async function start() {
  resetReconnect();
  closeSocket();
  setState('loading', { message: 'Connecting to the screen…' });
  let body;
  try {
    body = await terminalJsonApi('/api/screen/monitors');
  } catch (exc) {
    // The login overlay just went up under this top-layer dialog: step
    // aside so it can be used (and keep a pop-out window open for it).
    if (exc instanceof AuthRequiredError) { hideView(); return; }
    if (!view.open) return;
    showRefusal(exc && exc.status === 403
      ? 'The screen is Tailscale-only. Open the launcher over your Tailscale URL.'
      : "Couldn't reach the launcher.");
    return;
  }
  if (!view.open) return;
  view.monitors = (body && body.monitors) || [];
  if (!view.monitors.length) {
    setState('empty', { message: 'No display found on the PC.', glyph: 'monitor' });
    renderPicker();
    return;
  }
  if (!view.monitors.some(function (m) { return m.id === view.monitorId; })) {
    view.monitorId = chooseMonitor(view.monitors, readStoredMonitor()).id;
  }
  renderPicker();
  await connect();
}

function showRefusal(message, glyph) {
  setState('error', { message: message, glyph: glyph || 'lock', retry: start });
}

// The monitor last picked on this device, or null.
function readStoredMonitor() {
  try { return parseMonitorChoice(localStorage.getItem(MONITOR_KEY)); } catch (_) { return null; }
}

function storeMonitor(monitor) {
  try { localStorage.setItem(MONITOR_KEY, JSON.stringify(monitorChoice(monitor))); } catch (_) { /* storage blocked */ }
}

function forgetMonitor() {
  try { localStorage.removeItem(MONITOR_KEY); } catch (_) { /* storage blocked */ }
}

// --------------------------------------------------------------- picker

// "Main" for the primary, the others by where they sit relative to it.
export function monitorLabels(monitors) {
  const primary = monitors.find(function (m) { return m.primary; });
  const seen = {};
  return monitors.map(function (m, i) {
    let label;
    if (!primary) {
      label = 'Screen ' + (i + 1);
    } else if (m === primary) {
      label = 'Main';
    } else {
      const dx = (m.left + m.width / 2) - (primary.left + primary.width / 2);
      const dy = (m.top + m.height / 2) - (primary.top + primary.height / 2);
      if (Math.abs(dy) > Math.abs(dx)) label = dy < 0 ? 'Above' : 'Below';
      else label = dx < 0 ? 'Left' : 'Right';
    }
    seen[label] = (seen[label] || 0) + 1;
    return seen[label] > 1 ? label + ' ' + seen[label] : label;
  });
}

function renderPicker() {
  const picker = $('screenPicker');
  if (!picker) return;
  picker.replaceChildren();
  picker.hidden = view.monitors.length < 2;
  if (picker.hidden) return;
  const labels = monitorLabels(view.monitors);
  view.monitors.forEach(function (m, i) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'segmented-item';
    btn.textContent = labels[i];
    btn.dataset.monitor = String(m.id);
    btn.setAttribute('aria-label', labels[i] + ', ' + m.width + ' by ' + m.height);
    const active = m.id === view.monitorId;
    btn.setAttribute('aria-pressed', active ? 'true' : 'false');
    btn.addEventListener('click', function () {
      if (m.id === view.monitorId) return;
      view.monitorId = m.id;
      storeMonitor(m);
      resetReconnect();
      renderPicker();
      clearFrame();
      resetZoom();
      setState('loading', { message: 'Connecting to the screen…' });
      connect();
    });
    picker.appendChild(btn);
  });
}

// --------------------------------------------------------------- socket

function frameWidth() {
  const dpr = window.devicePixelRatio || 1;
  const longSide = Math.max(window.screen.width || 0, window.screen.height || 0);
  return Math.min(MAX_FRAME_WIDTH, Math.round(longSide * dpr) || MAX_FRAME_WIDTH);
}

function socketUrl(terminalToken) {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const params = new URLSearchParams();
  params.set('monitor', String(view.monitorId));
  params.set('width', String(frameWidth()));
  params.set('preset', view.preset);
  const bearer = readToken();
  if (bearer) params.set('token', bearer);
  if (terminalToken) params.set('tt', terminalToken);
  return proto + '//' + location.host + '/api/screen/ws?' + params.toString();
}

async function connect() {
  closeSocket();
  // A newer connect() or a teardown while the token is awaited makes this
  // one stale: two sockets must never be open at once.
  const gen = view.connectGen;
  let tt;
  try {
    tt = await ensureTerminalToken();
  } catch (exc) {
    if (gen === view.connectGen && view.open) showRefusal('Passkey unlock failed.');
    return;
  }
  if (gen !== view.connectGen || !view.open) return;
  const ws = new WebSocket(socketUrl(tt));
  ws.binaryType = 'blob';
  view.ws = ws;
  view.serverStats = null;
  ws.onopen = function () {
    // A new socket starts with the readout off on the server: say so again.
    if (ws === view.ws && view.readoutOn) sendReadoutSwitch(true);
  };
  ws.onmessage = function (event) {
    if (ws !== view.ws) return;
    if (typeof event.data === 'string') {
      onText(event.data);
      return;
    }
    drawFrame(ws, event.data);
  };
  ws.onclose = function (event) {
    if (ws !== view.ws) return;
    view.ws = null;
    onClose(event);
  };
}

function onText(text) {
  let msg = null;
  try { msg = JSON.parse(text); } catch (_) { return; }
  if (!msg) return;
  if (msg.type === 'status') onStatus(msg);
  else if (msg.type === 'cursor') onCursor(msg);
  else if (msg.type === 'clipboard') onClipboard(msg);
  else if (msg.type === 'stats') onStats(msg);
}

function onStatus(msg) {
  if (msg.state === 'unavailable') {
    setLive(false);
    if (view.frameUrl) {
      setState('stale', { message: staleMessage(view.lastFrameAt) });
    } else {
      setState('error', {
        message: "The PC's screen can't be read right now. It may be locked or asleep.",
        glyph: 'lock',
      });
    }
  } else if (msg.state === 'live' && !view.frameUrl) {
    setState('loading', { message: 'Connecting to the screen…' });
  }
}

function drawFrame(ws, blob) {
  const img = $('screenImage');
  const url = URL.createObjectURL(blob);
  noteReadoutFrame(blob.size);
  const ack = function () {
    if (ws === view.ws && ws.readyState === WebSocket.OPEN) {
      ws.send('ack');
      noteReadoutAck();
    }
  };
  img.onload = function () {
    // Decoded after its socket went away: it must not undo the Reconnecting
    // line or a refusal, nor mark a dead connection live.
    if (ws !== view.ws) {
      URL.revokeObjectURL(url);
      return;
    }
    const previous = view.frameUrl;
    view.frameUrl = url;
    view.lastFrameAt = Date.now();
    if (previous) URL.revokeObjectURL(previous);
    img.hidden = false;
    view.attempt = 0;
    setState('ready');
    setLive(true);
    ack();
  };
  img.onerror = function () {
    URL.revokeObjectURL(url);
    ack();
  };
  img.src = url;
}

function onClose(event) {
  setLive(false);
  // A drag in flight must not release into the next connection.
  dropGesture();
  if (!view.open) return;
  const reason = event && event.reason ? event.reason : '';
  if (event.code === 4403) {
    showRefusal('The screen is Tailscale-only. Open the launcher over your Tailscale URL.');
  } else if (event.code === 4401) {
    if (/passkey/i.test(reason)) clearTerminalToken();
    showRefusal(/passkey/i.test(reason) ? 'Passkey unlock required.' : 'Sign in again to view the screen.');
  } else if (event.code === 4404) {
    forgetMonitor();
    showRefusal('That display is gone. Retry to pick again.', 'monitor');
  } else {
    reconnectLater();
  }
}

// A new quality preset applies by reconnecting. The last frame stays under a
// quiet line until the new connection's first one is drawn.
function reconnectForPreset() {
  resetReconnect();
  setState(view.frameUrl ? 'stale' : 'loading', { message: 'Changing the quality…' });
  connect();
}

function clearRetryTimer() {
  if (view.retryTimer) clearTimeout(view.retryTimer);
  view.retryTimer = null;
}

function resetReconnect() {
  clearRetryTimer();
  view.attempt = 0;
}

// A drop that is not a refusal: try again after the next backoff step, with
// the last frame left under a quiet "Reconnecting…". A page that is hidden
// waits (coming back reconnects); a spent budget falls back to Retry.
function reconnectLater() {
  const delay = reconnectDelay(view.attempt);
  if (delay === null) {
    // The last frame stays behind the message, so it is still readable.
    setState('error', { message: 'Disconnected from the screen.', glyph: 'monitor', retry: start });
    return;
  }
  // Frame left: over it, quietly; none yet (the first connect failed): the
  // loading block.
  setState(view.frameUrl ? 'stale' : 'loading', { message: 'Reconnecting…' });
  if (document.hidden) return;
  view.attempt += 1;
  console.info('Screen: reconnect attempt ' + view.attempt + ' in ' + delay + ' ms');
  clearRetryTimer();
  view.retryTimer = setTimeout(function () {
    view.retryTimer = null;
    if (!view.open || document.hidden || view.ws) return;
    connect();
  }, delay);
}

function closeSocket() {
  const ws = view.ws;
  view.ws = null;
  view.connectGen += 1;
  clearRetryTimer();
  dropGesture();
  // Where the cursor was is not where it is: draw none until told again.
  view.cursor = null;
  resetClipboard();
  setLive(false);
  if (ws && ws.readyState <= WebSocket.OPEN) ws.close();
}

function clearFrame() {
  const img = $('screenImage');
  if (img) {
    img.hidden = true;
    img.removeAttribute('src');
  }
  if (view.frameUrl) URL.revokeObjectURL(view.frameUrl);
  view.frameUrl = '';
  view.lastFrameAt = 0;
  placeCursor();
}

function hideView() {
  view.open = false;
  setMode('view');
  view.pad = null;
  closeSocket();
  closePasteSheet();
  closeQualitySheet();
  setReadout(false);
  clearFrame();
  const dialog = $('screenView');
  if (dialog && dialog.open) dialog.close();
}

function closeView() {
  hideView();
  if (view.standalone) window.close();
}

function setLive(live) {
  view.live = live;
  const keys = $('screenKeys');
  if (keys) {
    keys.querySelectorAll('.key-btn').forEach(function (btn) { btn.disabled = !live; });
  }
  placeCursor();
  placeLoupe();
}

function setMode(mode) {
  if (mode !== 'control') {
    // Lift a drag in progress while input can still go out.
    cancelGesture();
    setKeysOpen(false);
  } else if (view.mode !== 'control') {
    // Into Control: the style last used on this device.
    let stored = null;
    try { stored = localStorage.getItem(CONTROL_KEY); } catch (_) { /* storage blocked */ }
    view.control = controlStyle(stored);
  }
  view.mode = mode;
  const dialog = $('screenView');
  if (dialog) dialog.dataset.mode = mode;
  const picker = $('screenMode');
  if (picker) {
    picker.querySelectorAll('.segmented-item').forEach(function (btn) {
      const active = btn.dataset.mode === mode;
      btn.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
  }
  const kb = $('screenKeyboard');
  if (kb) kb.hidden = mode !== 'control';
  renderControl();
}

// Touch or Trackpad, picked on the switch: remembered for the next time
// Control is picked on this device.
function setControl(style) {
  if (style === view.control) return;
  cancelGesture();
  view.control = controlStyle(style);
  try { localStorage.setItem(CONTROL_KEY, view.control); } catch (_) { /* storage blocked */ }
  renderControl();
}

function renderControl() {
  const dialog = $('screenView');
  if (dialog) dialog.dataset.control = view.control;
  const picker = $('screenControl');
  if (picker) {
    picker.hidden = view.mode !== 'control';
    picker.querySelectorAll('.segmented-item').forEach(function (btn) {
      const active = btn.dataset.control === view.control;
      btn.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
  }
  if (isPad()) {
    // The trackpad's cursor starts on the PC's, or mid-picture.
    if (!view.pad) view.pad = { x: 0.5, y: 0.5 };
    syncPad();
  }
  placeCursor();
}

// ----------------------------------------------------------- stage wiring

function wireStage(stage) {
  const pointers = new Map();
  let pinch = null;

  function local(e) {
    const r = stage.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top, t: e.timeStamp };
  }

  stage.addEventListener('pointerdown', function (e) {
    if (e.target.closest('button')) return;
    try { stage.setPointerCapture(e.pointerId); } catch (_) { /* synthetic */ }
    const pt = local(e);
    pointers.set(e.pointerId, pt);
    if (view.mode === 'control') {
      if (isPad()) padDown(pointers, e, pt); else controlDown(pointers, e, pt);
      return;
    }
    if (pointers.size === 2) {
      const p = twoFingerState(pointers);
      pinch = { start: p, scale: view.zoom.scale };
    }
  });
  stage.addEventListener('pointermove', function (e) {
    const prev = pointers.get(e.pointerId);
    if (!prev) return;
    const now = local(e);
    pointers.set(e.pointerId, now);
    if (view.mode === 'control') {
      if (isPad()) padMove(pointers, now); else controlMove(pointers, now);
      return;
    }
    if (pointers.size === 2 && pinch) {
      const p = twoFingerState(pointers);
      zoomAt(pinch.scale * (p.dist / pinch.start.dist), p.mid.x, p.mid.y);
    } else if (pointers.size === 1 && view.zoom.scale > 1) {
      view.zoom.x += now.x - prev.x;
      view.zoom.y += now.y - prev.y;
      applyZoom();
    }
  });
  const lift = function (e) {
    if (!pointers.has(e.pointerId)) return;
    const pt = local(e);
    pointers.delete(e.pointerId);
    if (pointers.size < 2) pinch = null;
    if (view.mode === 'control') {
      if (e.type === 'pointercancel') cancelGesture();
      else if (isPad()) padUp(pointers);
      else controlUp(pointers, pt);
    }
  };
  stage.addEventListener('pointerup', lift);
  stage.addEventListener('pointercancel', lift);
  stage.addEventListener('wheel', function (e) {
    e.preventDefault();
    const q = local(e);
    // A trackpad pinch arrives as a ctrl+wheel: always a local zoom.
    if (view.mode === 'control' && !e.ctrlKey) {
      const at = isPad() ? view.pad : framePoint(q, true);
      const wheel = wheelFromWheelEvent(e.deltaX, e.deltaY, e.deltaMode);
      if (at && (wheel.dx || wheel.dy)) {
        queueMove({ type: 'scroll', x: at.x, y: at.y, dx: wheel.dx, dy: wheel.dy });
      }
      return;
    }
    zoomAt(view.zoom.scale * Math.exp(-e.deltaY * 0.002), q.x, q.y);
  }, { passive: false });
  // A double-click resets the zoom in View; in Control it is the PC's.
  stage.addEventListener('dblclick', function () {
    if (view.mode === 'view') resetZoom();
  });
  stage.addEventListener('contextmenu', function (e) { e.preventDefault(); });
}

// ------------------------------------------------- landscape handle

// A phone held sideways hides the bar and the tools row behind the handle
// (styles/apps.css). The CSS owns the layout, so this only tracks whether the bars
// are up: a class on the dialog. Keep the query identical to the stylesheet's.
const SCREEN_LANDSCAPE = '(orientation: landscape) and (pointer: coarse) and (max-height: 500px)';

function chromeOpen() {
  const dialog = $('screenView');
  return !!dialog && dialog.classList.contains('chrome-open');
}

function setChrome(open) {
  const dialog = $('screenView');
  if (dialog) dialog.classList.toggle('chrome-open', open);
  const handle = $('screenHandle');
  if (handle) handle.setAttribute('aria-expanded', open ? 'true' : 'false');
}

function wireChrome(dialog, stage) {
  const handle = $('screenHandle');
  if (!handle) return;
  handle.addEventListener('click', function () { setChrome(!chromeOpen()); });
  // A touch on the picture while the bars are up puts them away and is only
  // that: it never also zooms, clicks or moves anything on the PC. Capture
  // phase, registered ahead of the stage's own listeners, so it runs first.
  if (stage) {
    stage.addEventListener('pointerdown', function (e) {
      if (!chromeOpen() || e.target.closest('button')) return;
      e.stopImmediatePropagation();
      setChrome(false);
    }, true);
  }
  // Picking a display, or raising the keyboard, finishes what the bars were
  // opened for. The mode and style switches stay up: the next tap is often
  // the keyboard or the style that a mode change just revealed.
  const picker = $('screenPicker');
  if (picker) {
    picker.addEventListener('click', function (e) {
      if (e.target.closest('.segmented-item')) setChrome(false);
    });
  }
  const keyboard = $('screenKeyboard');
  if (keyboard) keyboard.addEventListener('click', function () { setChrome(false); });
  // Closed with the view, and on a turn of the phone: the bars come back in
  // the flow (portrait) or start hidden (landscape), never half-open.
  dialog.addEventListener('close', function () { setChrome(false); });
  window.matchMedia(SCREEN_LANDSCAPE).addEventListener('change', function () { setChrome(false); });
}

// ----------------------------------------------------------------- wiring

export function wireScreenView() {
  const dialog = $('screenView');
  if (!dialog) return;
  const close = $('screenViewClose');
  if (close) close.addEventListener('click', closeView);
  // Esc closes the dialog natively; stop capture with it.
  dialog.addEventListener('close', function () {
    if (view.open) closeView();
  });
  const stage = $('screenStage');
  wireChrome(dialog, stage);
  if (stage) wireStage(stage);
  const modePicker = $('screenMode');
  if (modePicker) {
    modePicker.addEventListener('click', function (e) {
      const btn = e.target.closest('.segmented-item');
      if (btn && btn.dataset.mode && btn.dataset.mode !== view.mode) setMode(btn.dataset.mode);
    });
  }
  const controlPicker = $('screenControl');
  if (controlPicker) {
    controlPicker.addEventListener('click', function (e) {
      const btn = e.target.closest('.segmented-item');
      if (btn && btn.dataset.control) setControl(btn.dataset.control);
    });
  }
  wireControl({ onThreeFingerTap: toggleKeys });
  wireKeyboard();
  wireHardwareKeyboard();
  wirePasteSheet();
  wireQuality({ onPresetChange: reconnectForPreset });
  window.addEventListener('resize', applyZoom);
  if (window.visualViewport) {
    const refit = function () { fitToKeyboard(); applyZoom(); };
    window.visualViewport.addEventListener('resize', refit);
    window.visualViewport.addEventListener('scroll', refit);
  }
  // A hidden page is not a viewer: drop the socket so capture stops, and
  // pick the stream back up when the page returns.
  document.addEventListener('visibilitychange', function () {
    if (!view.open) return;
    if (document.hidden) closeSocket();
    else if (!view.ws && view.monitorId !== null) {
      view.attempt = 0;
      connect();
    }
  });
  const row = $('cpuSheetScreen');
  if (row) {
    row.addEventListener('click', function () {
      const sheet = $('cpuSheet');
      if (sheet && sheet.open) sheet.close();
      openScreen();
    });
  }
}
