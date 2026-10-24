/* Screen view (#1520): the PC's desktop, live, behind the terminal's gate.
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
 * Control (#1521). The View / Control switch picks what touches do. View
 * (the default on every open, so a stray tap never clicks) is the above.
 * Control sends input messages on the same socket: tap = click, a second
 * tap soon after = double click, long-press = right click, press-and-drag =
 * drag, two-finger drag = scroll wheel; a two-finger pinch still zooms (and
 * pans) locally. The keyboard toggle raises the phone keyboard through a
 * hidden input and shows the keys bar (Esc, Tab, arrows, Backspace, Del,
 * Enter, and Ctrl / Alt / Shift / Win, sticky for one key; a second page
 * holds F1-F12 and Home / End / PgUp / PgDn, #1602). A hardware keyboard's
 * non-text keys and Ctrl / Alt / Cmd shortcuts arrive on keydown (#1723). sendInput() is
 * the one way out, and it sends nothing in View mode or while the picture
 * is not live: input aimed at a stale frame would land somewhere else.
 * Coordinates are 0-1 within the frame (screen-gestures.js maps them); the
 * server turns them into PC pixels.
 *
 * Cursor (#1598). The picture never contains the PC's mouse cursor, so the
 * server sends it as its own `cursor` messages (0-1 in the frame, a shape
 * name, visible or not) whenever it moves, frame or no frame. placeCursor()
 * draws it over the picture with stagePoint(), outside the zoom transform,
 * so it keeps its size when zoomed; it never takes a touch, and it hides
 * off the picture, off the stage, while the picture is not live, and when
 * the PC hides it.
 *
 * Trackpad (#1599). Control has two styles, picked by the Touch / Trackpad
 * switch beside it: Touch is the above, Trackpad works like a laptop's. One
 * finger moves the cursor (view.pad, 0-1 in the frame) by how far it
 * travels, wherever it lands, and a move never clicks; a tap clicks at the
 * cursor, two taps double-click there, a long-press lifted without moving
 * and a two-finger tap right-click there, and a double-tap held then moved
 * drags with the left button held (the Windows remote desktop app's
 * gestures). Two fingers scroll at the cursor or pinch, and a zoomed
 * picture pans to keep the cursor in view. The style is remembered per device and applies when
 * Control is picked; the view still opens in View. The overlay draws the
 * server's cursor, except while the finger is moving it and its messages
 * lag behind: then the locally moved one, which is where it is going.
 *
 * Reconnect (#1603). A dropped socket is retried on its own, after 1 s, 2 s,
 * 4 s, 8 s and 15 s steps (screen-reconnect.js), with "Reconnecting…" over
 * the last frame; a drawn frame resets the count, and a spent budget falls
 * back to the Retry card. A refusal (4401, 4403, 4404) is never retried, and
 * neither is a hidden page or a closed view. Each attempt goes through
 * connect() like the first, so the passkey token, the Tailscale gate and the
 * tunnel refusal apply every time. The monitor picked is remembered per
 * device and reopened by its rectangle, then its id, then the primary.
 *
 * Clipboard (#1719). Two keys on the keys bar send the phone's clipboard text
 * to the PC: Paste to PC sets the PC's clipboard, Paste + Ctrl+V sets it and
 * then presses Ctrl+V. The text is read inside the tap (iOS asks for its own
 * Paste confirmation there) and goes out as one `clipboard_set` message
 * through sendInput, so it is refused in View mode and while the picture is
 * not live like any other input; if the phone refuses the read, a small sheet
 * takes the text by hand. The server answers each message with a `clipboard`
 * message (ok, or why not), shown as a short line over the picture. Nothing
 * here keeps, logs or counts what was copied.
 *
 * Quality (#1721). A button in the tools row opens a small sheet with three
 * presets (Sharp, Balanced, Data saver) and a readout switch. The preset rides
 * the connect query (`preset=`) and is remembered per device, so a pick
 * reconnects through connect() like the first open; what each preset means is
 * the server's table. The readout is an overlay with fps, kB/s, the average
 * frame size and the time from ack to the next frame (screen-quality.js does
 * the arithmetic over the frames received), plus the PC's per-stage times from
 * the server's `stats` message. Switching it on sends one small `readout`
 * control message (not input, so it works in View too); with it off the server
 * sends no `stats` at all. The ack protocol is untouched.
 *
 * Loupe (#1725). In Touch control the finger covers the spot it is about to
 * click, so while one finger is down (after 150 ms) and while it drags, a round
 * loupe above it shows the local frame under it at 2.5x. It is drawn from the
 * frame already in hand (no network), sits in its own overlay like the cursor,
 * and never takes a touch. Purely visual: no gesture, threshold or click point
 * depends on it. Hidden in View, in Trackpad, with two fingers down and on lift.
 */

import { isDesktopClient, readToken, AuthRequiredError } from './api.js';
import { clearTerminalToken, ensureTerminalToken, terminalJsonApi } from './webauthn.js';
import { setAsyncState, staleMessage } from './async-state.js';
import {
  GESTURE, LOUPE, allowKeyRepeat, anyMovedPastTap, classifyTwoFinger, comboKey, controlStyle, distance, followPan,
  frameFraction, hardwareKey, isDoubleTap, isMultiFingerTap, loupeLayout, movedPastTap, padStep, stagePoint, wheelFromDrag,
  wheelFromWheelEvent,
} from './screen-gestures.js';
import { cursorShape, cursorSvg } from './screen-cursor.js';
import {
  MONITOR_KEY, chooseMonitor, monitorChoice, parseMonitorChoice, reconnectDelay,
} from './screen-reconnect.js';
import {
  PRESETS, PRESET_KEY, Readout, presetChoice, presetNote, readoutLines,
} from './screen-quality.js';

const MAX_ZOOM = 5;
// The widest frame worth asking for: the phone's long side in device pixels,
// capped (the server never sends more than the monitor's own width).
const MAX_FRAME_WIDTH = 1920;
const POPUP_NAME = 'launcher-screen';
const POPUP_FEATURES = 'popup,width=1280,height=760';
// The last Control style used on this device (#1599).
const CONTROL_KEY = 'launcher.screenControlMode';

const view = {
  open: false,
  standalone: false,   // this window is the pop-out: closing closes it
  monitors: [],
  monitorId: null,
  ws: null,
  connectGen: 0,       // bumped by every socket teardown: a connect() older than it is stale
  attempt: 0,          // automatic reconnects tried since the last drawn frame
  retryTimer: null,    // the pending automatic reconnect
  frameUrl: '',
  lastFrameAt: 0,
  zoom: { scale: 1, x: 0, y: 0 },
  mode: 'view',        // 'view' | 'control'
  control: 'touch',    // Control's style: 'touch' | 'trackpad'
  live: false,         // a frame is drawn and the desktop is readable
  mods: new Set(),     // sticky modifiers for the next key
  cursor: null,        // the last cursor message: {x, y, shape, visible}
  pad: null,           // the trackpad's cursor, {x, y} 0-1 in the frame
  padSentAt: 0,        // when input that moves the PC's cursor last went out
  preset: 'balanced',  // the quality preset on the connect query (#1721)
  readoutOn: false,    // the readout overlay is up (and the server sends stats)
  readout: null,       // the client's Readout while it is up
  serverStats: null,   // the last `stats` message from the server
};

// The sentinel the hidden input holds, so Backspace on an "empty" field
// still fires an input event to forward.
const KEY_SENTINEL = ' ';

function $(id) { return document.getElementById(id); }

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

// The monitor last picked on this device (#1603), or null.
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

// ---------------------------------------------------------- zoom and pan

function resetZoom() {
  view.zoom = { scale: 1, x: 0, y: 0 };
  applyZoom();
}

function applyZoom() {
  const img = $('screenImage');
  const stage = $('screenStage');
  if (!img || !stage) return;
  const z = view.zoom;
  const w = stage.clientWidth;
  const h = stage.clientHeight;
  // Keep the zoomed picture covering the stage: no panning past an edge.
  z.x = Math.min(0, Math.max(w - w * z.scale, z.x));
  z.y = Math.min(0, Math.max(h - h * z.scale, z.y));
  img.style.transform = z.scale === 1 ? '' :
    'translate(' + z.x + 'px, ' + z.y + 'px) scale(' + z.scale + ')';
  placeCursor();
}

// Zoom to `scale` keeping the stage point (qx, qy) where it is.
function zoomAt(scale, qx, qy) {
  const z = view.zoom;
  const next = Math.min(MAX_ZOOM, Math.max(1, scale));
  z.x = qx - (qx - z.x) * (next / z.scale);
  z.y = qy - (qy - z.y) * (next / z.scale);
  z.scale = next;
  applyZoom();
}

// ----------------------------------------------------------------- cursor

function onCursor(msg) {
  view.cursor = msg;
  syncPad();
  placeCursor();
}

function isPad() {
  return view.mode === 'control' && view.control === 'trackpad';
}

// The trackpad's cursor leads while a finger moves it and until the
// server's messages have caught up with the last input sent.
function padLeads() {
  return (!!gesture && gesture.kind.startsWith('pad')) ||
    Date.now() - view.padSentAt < GESTURE.padLeadMs;
}

// Take the server's cursor as the trackpad's whenever it is not leading,
// so a move of the PC's own mouse is where the next drag starts.
function syncPad() {
  const c = view.cursor;
  if (!isPad() || padLeads() || !c || !c.visible) return;
  if (c.x >= 0 && c.x <= 1 && c.y >= 0 && c.y <= 1) view.pad = { x: c.x, y: c.y };
}

// The cursor to draw: the server's, or in trackpad mode the local one while
// it leads or the server has none to show (it must always be visible there).
function drawnCursor() {
  const c = view.cursor && view.cursor.visible ? view.cursor : null;
  if (!isPad() || !view.pad || (c && !padLeads())) return c;
  return { x: view.pad.x, y: view.pad.y, shape: c ? c.shape : 'arrow', visible: true };
}

// Draw the cursor over the picture, or hide it when there is none, it is
// hidden, its spot is not on screen, or the picture is not live (a locked
// PC's cursor is not drawn over its last frame).
function placeCursor() {
  const el = $('screenCursor');
  if (!el) return;
  const img = $('screenImage');
  const stage = $('screenStage');
  const c = drawnCursor();
  const at = c && c.visible && view.live && img && stage && !img.hidden
    ? stagePoint(c.x, c.y, view.zoom, stage.clientWidth, stage.clientHeight,
      img.naturalWidth, img.naturalHeight)
    : null;
  if (!at) {
    el.hidden = true;
    return;
  }
  const shape = cursorShape(c.shape);
  if (el.dataset.shape !== shape) {
    el.innerHTML = cursorSvg(shape);
    el.dataset.shape = shape;
  }
  el.style.transform = 'translate(' + at.x + 'px, ' + at.y + 'px)';
  el.hidden = false;
}

// ---------------------------------------------------------------- control

// The one way input leaves the view: Control mode, an open socket, and a
// live picture, or nothing is sent.
function canSend() {
  const ws = view.ws;
  return view.mode === 'control' && view.live && !!ws && ws.readyState === WebSocket.OPEN;
}

function sendInput(msg) {
  if (!canSend()) return false;
  view.ws.send(JSON.stringify(msg));
  if ('x' in msg) notePadSent();
  return true;
}

// Input that moves the PC's cursor just went out: the trackpad's cursor
// leads for a while, then the server's takes over again.
let padSettle = null;
function notePadSent() {
  view.padSentAt = Date.now();
  if (padSettle) clearTimeout(padSettle);
  padSettle = setTimeout(function () {
    padSettle = null;
    syncPad();
    placeCursor();
  }, GESTURE.padLeadMs);
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

// Where a stage point lands on the frame, 0-1, or null off the picture.
function framePoint(pt, clamp) {
  const img = $('screenImage');
  const stage = $('screenStage');
  if (!img || !stage || img.hidden) return null;
  return frameFraction(pt.x, pt.y, view.zoom, stage.clientWidth, stage.clientHeight,
    img.naturalWidth, img.naturalHeight, clamp);
}

// The gesture in flight in Control mode (null when none):
//   {kind: 'press', start, timer}        one finger, still a tap/long-press
//   {kind: 'drag', last}                 the left button is down on the PC
//   {kind: 'two', start, lastMid, scale0, cls, t0}  two fingers
//   {kind: 'three', starts, moved, t0}   three fingers: only a tap does anything
//   {kind: 'spent'}                      done; wait for every finger to lift
let gesture = null;
let lastTap = null;
let pendingMove = null;

function flushMove() {
  const msg = pendingMove;
  pendingMove = null;
  if (msg) sendInput(msg);
}

// Moves and scrolls coalesce to one message per animation frame.
function queueMove(msg) {
  const scheduled = pendingMove !== null;
  if (scheduled && msg.type === 'scroll' && pendingMove.type === 'scroll') {
    msg = Object.assign({}, msg, { dx: msg.dx + pendingMove.dx, dy: msg.dy + pendingMove.dy });
  } else if (scheduled && pendingMove.type !== msg.type) {
    flushMove();
    return queueMove(msg);
  }
  pendingMove = msg;
  if (!scheduled) requestAnimationFrame(flushMove);
}

// The connection is going: forget the gesture in flight without sending
// anything. The host lifts a held button itself when the socket closes, and an
// `up` sent after a reconnect would land on a picture the finger never pressed.
function dropGesture() {
  if (gesture && gesture.timer) clearTimeout(gesture.timer);
  gesture = null;
  lastTap = null;
  pendingMove = null;
  if (padTap) clearTimeout(padTap.timer);
  padTap = null;
  showHeld(false);
  placeLoupe();
}

function cancelGesture() {
  // A tap's click waiting on a second touch goes out first.
  flushPadTap();
  if (!gesture) return;
  // A touch that began inside a tap's wait: that tap still clicks.
  if (gesture.kind === 'pad' && gesture.second) {
    sendInput({ type: 'click', button: 'left', x: gesture.second.x, y: gesture.second.y });
  }
  if (gesture.timer) clearTimeout(gesture.timer);
  if (gesture.kind === 'drag') {
    flushMove();
    const at = framePoint(gesture.last, true);
    if (at) sendInput({ type: 'up', button: 'left', x: at.x, y: at.y });
  } else if (gesture.kind === 'pad-drag' && view.pad) {
    flushMove();
    sendInput({ type: 'up', button: 'left', x: view.pad.x, y: view.pad.y });
  }
  gesture = null;
  showHeld(false);
  placeLoupe();
}

function controlDown(pointers, e, pt) {
  if (pointers.size === 1) {
    if (e.pointerType === 'mouse' && e.button === 2) {
      const at = framePoint(pt);
      if (at) sendInput({ type: 'click', button: 'right', x: at.x, y: at.y });
      gesture = { kind: 'spent' };
      return;
    }
    gesture = {
      kind: 'press',
      start: pt,
      timer: setTimeout(function () {
        if (!gesture || gesture.kind !== 'press') return;
        const at = framePoint(gesture.start);
        if (at) sendInput({ type: 'click', button: 'right', x: at.x, y: at.y });
        gesture = { kind: 'spent' };
        placeLoupe();
      }, GESTURE.longPressMs),
    };
    armLoupe(e, pt);
  } else if (pointers.size === 2 || pointers.size === 3) {
    startMultiFinger(pointers);
  }
}

// A second finger lands: whatever one finger was doing is over, and two
// fingers pinch, scroll or (lifted quickly) right-click. A third finger
// ends that too and only a quick tap of all three does anything: raise the
// keyboard (#1600). Both Control styles share this.
function startMultiFinger(pointers) {
  cancelGesture();
  const t0 = Date.now();
  if (pointers.size === 3) {
    gesture = { kind: 'three', starts: Array.from(pointers.values()), moved: false, t0: t0 };
    return;
  }
  const p = twoFingerState(pointers);
  gesture = { kind: 'two', start: p, lastMid: p.mid, scale0: view.zoom.scale, cls: null, t0: t0 };
}

// The first of two or three fingers lifts. A quick tap that never moved is
// a two-finger right-click (where the fingers were in Touch, at the cursor
// in Trackpad) or a three-finger keyboard toggle; anything else sends
// nothing, and the fingers still down must not start a drag.
function endMultiFinger() {
  const g = gesture;
  gesture = { kind: 'spent' };
  if (!isMultiFingerTap(g.kind === 'three' ? g.moved : !!g.cls, Date.now() - g.t0)) return;
  if (g.kind === 'three') {
    toggleKeys();
    return;
  }
  const at = isPad() ? view.pad : framePoint(g.lastMid);
  if (at) sendInput({ type: 'click', button: 'right', x: at.x, y: at.y });
}

function controlMove(pointers, pt) {
  if (!gesture) return;
  followLoupe(pt);
  if (gesture.kind === 'press') {
    if (!movedPastTap(gesture.start, pt)) return;
    clearTimeout(gesture.timer);
    const at = framePoint(gesture.start);
    if (!at) { gesture = { kind: 'spent' }; placeLoupe(); return; }
    sendInput({ type: 'down', button: 'left', x: at.x, y: at.y });
    gesture = { kind: 'drag', last: pt };
  }
  if (gesture.kind === 'drag') {
    gesture.last = pt;
    const at = framePoint(pt, true);
    if (at) queueMove({ type: 'move', x: at.x, y: at.y });
    return;
  }
  twoFingerMove(pointers);
}

// Two fingers, in either Control style: a pinch zooms and pans locally, a
// drag scrolls the PC's wheel under the fingers (Touch) or at the cursor
// (Trackpad).
function twoFingerMove(pointers) {
  if (gesture.kind === 'three') {
    // Three fingers never scroll, drag or click: moving just stops it
    // being a tap.
    gesture.moved = gesture.moved || anyMovedPastTap(gesture.starts, Array.from(pointers.values()));
    return;
  }
  if (gesture.kind !== 'two' || pointers.size !== 2) return;
  const now = twoFingerState(pointers);
  if (!gesture.cls) gesture.cls = classifyTwoFinger(gesture.start, now);
  if (gesture.cls === 'pinch') {
    pinchTo(gesture.scale0, gesture.start, now, gesture.lastMid);
  } else if (gesture.cls === 'scroll') {
    const at = isPad() ? view.pad : framePoint(now.mid, true);
    const wheel = wheelFromDrag(now.mid.x - gesture.lastMid.x, now.mid.y - gesture.lastMid.y);
    if (at && (wheel.dx || wheel.dy)) {
      queueMove({ type: 'scroll', x: at.x, y: at.y, dx: wheel.dx, dy: wheel.dy });
    }
  }
  gesture.lastMid = now.mid;
}

function controlUp(pointers, pt) {
  if (!gesture) return;
  if (gesture.kind === 'press') {
    clearTimeout(gesture.timer);
    const at = framePoint(gesture.start);
    if (at) {
      const tap = { t: Date.now(), x: gesture.start.x, y: gesture.start.y, at: at };
      // The second click of a double-tap goes where the first did, so
      // Windows sees two clicks on one pixel: a double click.
      const target = isDoubleTap(lastTap, tap) ? lastTap.at : at;
      sendInput({ type: 'click', button: 'left', x: target.x, y: target.y });
      lastTap = target === at ? tap : null;
    }
    gesture = null;
  } else if (gesture.kind === 'drag') {
    gesture.last = pt;
    cancelGesture();
  } else if (gesture.kind === 'two' || gesture.kind === 'three') {
    endMultiFinger();
  }
  if (pointers.size === 0) gesture = null;
  placeLoupe();
}

// ---------------------------------------------------------------- trackpad

function showHeld(held) {
  const el = $('screenCursor');
  if (el) el.classList.toggle('held', held);
}

// The picture's drawn size, unzoomed (object-fit: contain), or null.
function drawnSize() {
  const img = $('screenImage');
  const stage = $('screenStage');
  if (!img || !stage || img.hidden || !(img.naturalWidth > 0 && img.naturalHeight > 0)) return null;
  const fit = Math.min(stage.clientWidth / img.naturalWidth, stage.clientHeight / img.naturalHeight);
  return { w: img.naturalWidth * fit, h: img.naturalHeight * fit };
}

// Put the trackpad's cursor at `pad`, draw it, and pan a zoomed picture so
// it stays in view.
function setPad(pad) {
  view.pad = pad;
  const stage = $('screenStage');
  const img = $('screenImage');
  if (view.zoom.scale > 1 && stage && img) {
    const base = stagePoint(pad.x, pad.y, { scale: 1, x: 0, y: 0 }, stage.clientWidth,
      stage.clientHeight, img.naturalWidth, img.naturalHeight);
    if (base) {
      const pan = followPan(view.zoom, base.x, base.y, stage.clientWidth, stage.clientHeight,
        GESTURE.padFollowMargin);
      view.zoom.x = pan.x;
      view.zoom.y = pan.y;
      applyZoom();   // clamps the pan, and draws the cursor
      return;
    }
  }
  placeCursor();
}

// The finger moved from `from` to `to`: move the cursor by as much, scaled,
// and the PC's with it. Nothing moves at a stale picture.
function movePad(from, to) {
  const size = drawnSize();
  if (!view.live || !view.pad || !size) return;
  setPad(padStep(view.pad, to.x - from.x, to.y - from.y, to.t - from.t,
    size.w, size.h, view.zoom.scale));
  queueMove({ type: 'move', x: view.pad.x, y: view.pad.y });
}

// Trackpad gestures, after the Windows remote desktop app's mouse-pointer
// mode (#1599). Touching never clicks by itself: a touch that moves only
// moves the cursor, however long it rested first. A clean tap clicks, two
// quick taps double-click, a long-press lifted without moving right-clicks,
// as does a two-finger tap, and a double-tap held then moved drags with the
// left button down. A tap's click waits padTapWaitMs for a second touch,
// so a double-tap drag never clicks first.
//
// The gesture in flight in Trackpad (besides 'two' and 'spent' above):
//   {kind: 'pad', start, timer, held, second}  one finger that has not
//       moved yet: a tap or long-press so far. `held` once the long-press
//       time passes; `second` is the earlier tap's spot when this touch
//       began inside its wait, its click still unsent
//   {kind: 'pad-move', last}   moving the cursor, no button
//   {kind: 'pad-drag', last}   the left button is down at the cursor
let padTap = null;   // a tap's click waiting on a second touch: {at, timer}

function flushPadTap() {
  if (!padTap) return;
  clearTimeout(padTap.timer);
  const at = padTap.at;
  padTap = null;
  sendInput({ type: 'click', button: 'left', x: at.x, y: at.y });
}

function padClick(button, at) {
  sendInput({ type: 'click', button: button, x: at.x, y: at.y });
}

function padDown(pointers, e, pt) {
  if (pointers.size === 1) {
    if (!view.pad) view.pad = { x: 0.5, y: 0.5 };
    if (e.pointerType === 'mouse' && e.button === 2) {
      flushPadTap();
      padClick('right', view.pad);
      gesture = { kind: 'spent' };
      return;
    }
    // Inside a tap's wait: what this touch becomes decides that click.
    let second = null;
    if (padTap) {
      clearTimeout(padTap.timer);
      second = padTap.at;
      padTap = null;
    }
    gesture = {
      kind: 'pad',
      start: pt,
      held: false,
      second: second,
      timer: setTimeout(function () {
        if (!gesture || gesture.kind !== 'pad') return;
        gesture.held = true;
        gesture.timer = null;
        showHeld(true);
      }, GESTURE.longPressMs),
    };
  } else if (pointers.size === 2 || pointers.size === 3) {
    startMultiFinger(pointers);
  }
}

function padMove(pointers, pt) {
  if (!gesture) return;
  if (gesture.kind === 'pad') {
    // Still a tap or long-press: the cursor stays put.
    if (!movedPastTap(gesture.start, pt)) return;
    if (gesture.timer) clearTimeout(gesture.timer);
    showHeld(false);
    if (gesture.second) {
      // A double-tap held, then moved: a drag from the cursor.
      flushMove();
      if (!view.pad || !sendInput({ type: 'down', button: 'left', x: view.pad.x, y: view.pad.y })) {
        gesture = { kind: 'spent' };
        return;
      }
      gesture = { kind: 'pad-drag', last: pt };
    } else {
      gesture = { kind: 'pad-move', last: pt };
    }
    // The cursor moves from here on, so it doesn't jump by the slop.
    return;
  }
  if (gesture.kind === 'pad-move' || gesture.kind === 'pad-drag') {
    movePad(gesture.last, pt);
    gesture.last = pt;
    return;
  }
  twoFingerMove(pointers);
}

function padUp(pointers) {
  if (!gesture) return;
  if (gesture.kind === 'pad') {
    const g = gesture;
    if (g.timer) clearTimeout(g.timer);
    gesture = null;
    showHeld(false);
    if (g.second) padClick('left', g.second);   // the earlier tap's own click
    if (g.held) {
      padClick('right', view.pad);
    } else if (g.second) {
      padClick('left', g.second);   // its partner: a double click
    } else {
      const at = view.pad;
      padTap = { at: at, timer: setTimeout(flushPadTap, GESTURE.padTapWaitMs) };
    }
  } else if (gesture.kind === 'pad-drag') {
    cancelGesture();
  } else if (gesture.kind === 'pad-move') {
    gesture = null;
  } else if (gesture.kind === 'two' || gesture.kind === 'three') {
    endMultiFinger();
  }
  if (pointers.size === 0) gesture = null;
}

function twoFingerState(pointers) {
  const [a, b] = Array.from(pointers.values());
  return {
    dist: distance(a, b) || 1,
    mid: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 },
  };
}

// Pinch from the gesture's start: zoom by the distance ratio around the
// midpoint, and pan with the midpoint (Control's only way to pan).
function pinchTo(scale0, start, now, lastMid) {
  zoomAt(scale0 * (now.dist / start.dist), now.mid.x, now.mid.y);
  if (lastMid) {
    view.zoom.x += now.mid.x - lastMid.x;
    view.zoom.y += now.mid.y - lastMid.y;
    applyZoom();
  }
}

// ---------------------------------------------------------------- keyboard

// The keyboard button's own toggle, which a three-finger tap shares.
function toggleKeys() {
  const keys = $('screenKeys');
  setKeysOpen(!keys || keys.hidden);
}

function setKeysOpen(open) {
  const keys = $('screenKeys');
  const kb = $('screenKeyboard');
  const input = $('screenKeyInput');
  const dialog = $('screenView');
  if (!keys) return;
  keys.hidden = !open;
  if (dialog) dialog.classList.toggle('keys-open', open);
  if (kb) kb.setAttribute('aria-pressed', open ? 'true' : 'false');
  if (input) {
    if (open) {
      // Inside the tap's own handler, so iOS raises the keyboard.
      input.focus({ preventScroll: true });
      resetInput(input);
    } else {
      input.blur();
    }
  }
  if (!open) {
    clearMods();
    setKeysPage('main');
  }
  fitToKeyboard();
  applyZoom();
}

// The sentinel, with the caret after it, so a Backspace always has
// something to delete and fires.
function resetInput(input) {
  input.value = KEY_SENTINEL;
  try { input.setSelectionRange(KEY_SENTINEL.length, KEY_SENTINEL.length); } catch (_) { /* not focusable yet */ }
}

function clearMods() {
  view.mods.clear();
  renderMods();
}

function renderMods() {
  const keys = $('screenKeys');
  if (!keys) return;
  keys.querySelectorAll('[data-mod]').forEach(function (btn) {
    const held = view.mods.has(btn.dataset.mod);
    btn.classList.toggle('active', held);
    btn.setAttribute('aria-pressed', held ? 'true' : 'false');
  });
  // The F-keys page has no modifier keys of its own: its way back names
  // the ones still held, so Ctrl + F5 can be checked before the F5.
  const names = MOD_ORDER.filter(function (m) { return view.mods.has(m); })
    .map(function (m) { return MOD_LABEL[m]; });
  keys.querySelectorAll('[data-held]').forEach(function (el) {
    el.textContent = names.join('+');
    el.hidden = !names.length;
  });
}

// Which page of keys is up (#1602): 'main', the original keys, or 'fn', the
// F-keys and the navigation cluster. view.mods is not touched, so a modifier
// tapped on one page is still held on the other.
function setKeysPage(page) {
  const keys = $('screenKeys');
  if (!keys) return;
  keys.querySelectorAll('[data-keys-page]').forEach(function (el) {
    el.hidden = el.dataset.keysPage !== page;
  });
}

// Client-side macros: one key message, no server change. Task Manager is
// Ctrl+Shift+Esc; Ctrl+Alt+Del is the secure attention sequence and cannot be
// sent by an app. A macro sends exactly its own modifiers, so held sticky ones
// are not mixed in; they let go with it, as they do after any key.
const MACROS = {
  taskmgr: { key: 'esc', mods: ['ctrl', 'shift'] },
};
const MOD_ORDER = ['ctrl', 'alt', 'shift', 'win'];
const MOD_LABEL = { ctrl: 'Ctrl', alt: 'Alt', shift: 'Shift', win: 'Win' };

function sendMacro(name) {
  const macro = MACROS[name];
  if (!macro) return;
  if (sendInput({ type: 'key', key: macro.key, mods: macro.mods.slice() }) && view.mods.size) clearMods();
}

// A key with the sticky modifiers, which then let go: one key each.
function sendKey(key) {
  const mods = Array.from(view.mods);
  if (sendInput({ type: 'key', key: key, mods: mods }) && mods.length) clearMods();
}

function sendTyped(text) {
  if (!text) return;
  const combo = view.mods.size ? comboKey(text) : null;
  if (combo) {
    sendKey(combo);
    return;
  }
  if (sendInput({ type: 'text', text: text })) clearMods();
}

function wireKeyboard() {
  const kb = $('screenKeyboard');
  const keys = $('screenKeys');
  const input = $('screenKeyInput');
  if (kb) {
    kb.addEventListener('click', toggleKeys);
  }
  if (keys) {
    // Keep the phone keyboard up: a key tap must not take focus off the
    // hidden input.
    keys.addEventListener('pointerdown', function (e) {
      if (e.target.closest('.key-btn')) e.preventDefault();
    });
    keys.addEventListener('click', function (e) {
      const btn = e.target.closest('.key-btn');
      if (!btn || btn.disabled) return;
      if (input && document.activeElement !== input) {
        input.focus({ preventScroll: true });
        resetInput(input);
      }
      if (btn.dataset.mod) {
        const mod = btn.dataset.mod;
        if (view.mods.has(mod)) view.mods.delete(mod); else view.mods.add(mod);
        renderMods();
        return;
      }
      if (btn.dataset.pageTo) {
        setKeysPage(btn.dataset.pageTo);
        return;
      }
      if (btn.dataset.macro) {
        sendMacro(btn.dataset.macro);
        return;
      }
      if (btn.dataset.clip) {
        pasteToPc(btn.dataset.clip === 'paste');
        return;
      }
      if (btn.dataset.key) sendKey(btn.dataset.key);
    });
  }
  if (!input) return;
  // beforeinput, not keydown, for text: a phone keyboard's IME reports keyCode 229 for
  // every key, but names what it does here. Composed text (a dictation, a
  // CJK or swipe IME) can't be cancelled mid-flight; it goes out whole at
  // compositionend.
  input.addEventListener('beforeinput', function (e) {
    if (e.isComposing || e.inputType === 'insertCompositionText') return;
    e.preventDefault();
    switch (e.inputType) {
      case 'insertText':
        sendTyped(e.data || '');
        break;
      case 'insertLineBreak':
      case 'insertParagraph':
        sendKey('enter');
        break;
      case 'deleteContentBackward':
        sendKey('backspace');
        break;
      case 'deleteContentForward':
        sendKey('delete');
        break;
      default:
        break;
    }
  });
  input.addEventListener('compositionend', function (e) {
    sendTyped(e.data || '');
    resetInput(input);
  });
  // An edit that skipped beforeinput (some keyboards insert text without
  // one): forward what changed against the sentinel, then put it back. A
  // cancelled beforeinput fires no input event, so nothing goes twice.
  input.addEventListener('input', function (e) {
    if (e.isComposing || input.value === KEY_SENTINEL) return;
    const value = input.value;
    if (value.startsWith(KEY_SENTINEL)) sendTyped(value.slice(KEY_SENTINEL.length));
    else if (value === '') sendKey('backspace');
    resetInput(input);
  });
}

// ------------------------------------------------------ hardware keyboard

// A hardware keyboard (an iPad's, or the pop-out window's) presses keys the
// hidden input's beforeinput never sees: arrows, Esc, Tab, F-keys, Ctrl
// shortcuts (#1723). keydown carries them, where screen-gestures.js's
// hardwareKey decides what each one is; a plain printable character is left
// to beforeinput so nothing goes twice, and so is a phone IME's composition
// (keyCode 229). Only a key that was sent is cancelled, so a View-mode Esc
// still closes the dialog and an unsent F5 still reloads.
const keyRepeat = { count: 0, at: 0 };

function onHardwareKeydown(e) {
  const hit = hardwareKey(e);
  if (!hit || !canSend()) return;
  const target = e.target;
  const tag = target && target.tagName;
  // A focused field or button keeps its own typing keys; Enter and Space on a
  // button press it.
  if ((tag === 'INPUT' && target.id !== 'screenKeyInput') || tag === 'TEXTAREA' || tag === 'SELECT') return;
  if (tag === 'BUTTON' && !hit.mods.length && (hit.key === 'enter' || hit.key === 'space')) return;
  if (e.repeat) {
    keyRepeat.count += 1;
    const t = Date.now();
    const allowed = allowKeyRepeat(keyRepeat.count, t - keyRepeat.at);
    if (allowed) keyRepeat.at = t;
    e.preventDefault();
    if (!allowed) return;
  } else {
    keyRepeat.count = 0;
    keyRepeat.at = Date.now();
  }
  // The bar's sticky modifiers combine with a hardware key, as with typing.
  const mods = MOD_ORDER.filter(function (m) { return hit.mods.includes(m) || view.mods.has(m); });
  if (sendInput({ type: 'key', key: hit.key, mods: mods })) {
    e.preventDefault();
    if (view.mods.size) clearMods();
  }
}

function wireHardwareKeyboard() {
  const dialog = $('screenView');
  if (dialog) dialog.addEventListener('keydown', onHardwareKeydown);
}

// The phone keyboard covers the bottom of the screen without resizing the
// layout viewport, so while the keys are up the view follows the visual
// viewport and the keys bar sits on top of the keyboard.
function fitToKeyboard() {
  const dialog = $('screenView');
  const vv = window.visualViewport;
  if (!dialog) return;
  const keys = $('screenKeys');
  if (!vv || !keys || keys.hidden || !view.open) {
    dialog.style.top = '';
    dialog.style.height = '';
    return;
  }
  dialog.style.top = vv.offsetTop + 'px';
  dialog.style.height = vv.height + 'px';
}

// ----------------------------------------------------------- clipboard (#1719)

// The most text one transfer carries, in UTF-8 bytes: the server's own cap
// (src/desktop_clipboard.py MAX_CLIPBOARD_BYTES), checked here first so a
// huge paste is never sent just to be refused.
const CLIPBOARD_MAX_BYTES = 64 * 1024;
const CLIPBOARD_FAILURES = {
  busy: "The PC's clipboard is busy. Try again.",
  too_large: 'That text is too long to send (64 KB at most).',
  empty: 'There was no text to send.',
  error: "The PC's clipboard refused the text.",
};
const clipQueue = [];     // one entry per clipboard_set in flight: press Ctrl+V once it lands?
let noticeTimer = null;
let pasteSheetThenPaste = false;

// A short result line over the picture. The app's toast sits behind a modal
// dialog's top layer, so the view carries its own.
function showNotice(message, isError) {
  const el = $('screenNotice');
  if (!el) return;
  el.textContent = message;
  el.classList.toggle('error', !!isError);
  el.hidden = false;
  if (noticeTimer) clearTimeout(noticeTimer);
  noticeTimer = setTimeout(function () {
    noticeTimer = null;
    el.hidden = true;
  }, isError ? 4500 : 2200);
}

// A new connection or a closed one owes no answers, and shows no old one.
function resetClipboard() {
  clipQueue.length = 0;
  if (noticeTimer) clearTimeout(noticeTimer);
  noticeTimer = null;
  const el = $('screenNotice');
  if (el) el.hidden = true;
}

// The key's tap. readText() is called straight from the tap, before anything
// is awaited, so the phone treats it as the user's own action.
function pasteToPc(thenPaste) {
  if (!canSend()) return;
  const clip = navigator.clipboard;
  if (!clip || typeof clip.readText !== 'function') {
    openPasteSheet(thenPaste);
    return;
  }
  clip.readText().then(function (text) {
    if (!text) {
      showNotice("The phone's clipboard has no text");
      return;
    }
    sendClipboard(text, thenPaste);
  }, function () {
    // Refused (permission denied, or the page was not trusted with it).
    openPasteSheet(thenPaste);
  });
}

function sendClipboard(text, thenPaste) {
  if (new TextEncoder().encode(text).length > CLIPBOARD_MAX_BYTES) {
    showNotice(CLIPBOARD_FAILURES.too_large, true);
    return;
  }
  if (sendInput({ type: 'clipboard_set', text: text })) {
    clipQueue.push(!!thenPaste);
  } else {
    showNotice('Wait for the picture to be live, in Control, to paste.', true);
  }
}

// The server's answer to one clipboard_set, in the order they were sent.
function onClipboard(msg) {
  const thenPaste = clipQueue.shift() === true;
  if (!msg.ok) {
    showNotice(CLIPBOARD_FAILURES[msg.reason] || CLIPBOARD_FAILURES.error, true);
    return;
  }
  // Ctrl+V only after the copy landed: a failed copy must never paste what
  // the PC's clipboard held before. Exactly Ctrl, like a macro: held sticky
  // modifiers let go with it.
  if (thenPaste && sendInput({ type: 'key', key: 'v', mods: ['ctrl'] })) {
    if (view.mods.size) clearMods();
    showNotice('Pasted on the PC');
    return;
  }
  showNotice('Sent to the PC clipboard');
}

function openPasteSheet(thenPaste) {
  const sheet = $('screenPasteSheet');
  if (!sheet) return;
  pasteSheetThenPaste = !!thenPaste;
  const field = $('screenPasteText');
  if (field) field.value = '';
  if (!sheet.open) sheet.showModal();
}

function closePasteSheet() {
  const sheet = $('screenPasteSheet');
  if (sheet && sheet.open) sheet.close();
}

function wirePasteSheet() {
  const sheet = $('screenPasteSheet');
  if (!sheet) return;
  const close = $('screenPasteClose');
  if (close) close.addEventListener('click', closePasteSheet);
  const form = $('screenPasteForm');
  if (form) {
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      const field = $('screenPasteText');
      const text = field ? field.value : '';
      closePasteSheet();
      if (text) sendClipboard(text, pasteSheetThenPaste);
    });
  }
  // A tap on the backdrop lands on the <dialog> itself, outside the card.
  sheet.addEventListener('click', function (e) {
    if (e.target === sheet) closePasteSheet();
  });
}

// ------------------------------------------------------- quality (#1721)

const READOUT_REFRESH_MS = 500;
let readoutTimer = null;

// The preset last picked on this device, or Balanced.
function loadPreset() {
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
  resetReconnect();
  setState(view.frameUrl ? 'stale' : 'loading', { message: 'Changing the quality…' });
  connect();
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
function sendReadoutSwitch(on) {
  const ws = view.ws;
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'readout', on: on }));
  }
}

function setReadout(on) {
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

function noteReadoutFrame(bytes) {
  if (view.readout) view.readout.frame(bytes, performance.now());
}

function noteReadoutAck() {
  if (view.readout) view.readout.acked(performance.now());
}

function onStats(msg) {
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

function closeQualitySheet() {
  const sheet = $('screenQualitySheet');
  if (sheet && sheet.open) sheet.close();
}

function wireQuality() {
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

// ----------------------------------------------------------------- loupe

// The loupe's state: the arming timer, whether the 150 ms have passed, and
// the finger's latest point (stage-local). Whether it shows is derived from
// the view and the gesture each time (placeLoupe), so a lift, a cancel, a
// second finger or a mode change hides it without a hide call of its own.
const loupe = { timer: null, shown: false, pt: null };

// One finger landed in Touch control: show the loupe after LOUPE.delayMs. A
// mouse pointer covers nothing, so it gets none.
function armLoupe(e, pt) {
  disarmLoupe();
  if (e.pointerType === 'mouse') return;
  loupe.pt = pt;
  loupe.timer = setTimeout(function () {
    loupe.timer = null;
    loupe.shown = true;
    placeLoupe();
  }, LOUPE.delayMs);
}

function disarmLoupe() {
  if (loupe.timer) clearTimeout(loupe.timer);
  loupe.timer = null;
  loupe.shown = false;
}

function followLoupe(pt) {
  loupe.pt = pt;
  placeLoupe();
}

// Draw the loupe over the finger, or hide it. It is up only while ONE finger
// is down in Touch control (a press still deciding, or a drag) on a live
// picture.
function placeLoupe() {
  const el = $('screenLoupe');
  if (!el) return;
  const one = !!gesture && (gesture.kind === 'press' || gesture.kind === 'drag');
  if (!one) disarmLoupe();
  const img = $('screenImage');
  const stage = $('screenStage');
  const at = one && loupe.shown && loupe.pt && view.mode === 'control' && view.control === 'touch' &&
    view.live && view.frameUrl && img && stage && !img.hidden
    ? loupeLayout(loupe.pt, view.zoom, stage.clientWidth, stage.clientHeight,
      img.naturalWidth, img.naturalHeight)
    : null;
  if (!at) {
    el.hidden = true;
    return;
  }
  if (el.dataset.frame !== view.frameUrl) {
    el.style.backgroundImage = 'url("' + view.frameUrl + '")';
    el.dataset.frame = view.frameUrl;
  }
  el.style.width = at.size + 'px';
  el.style.height = at.size + 'px';
  el.style.backgroundSize = at.bgW + 'px ' + at.bgH + 'px';
  el.style.backgroundPosition = at.bgX + 'px ' + at.bgY + 'px';
  el.style.transform = 'translate(' + at.x + 'px, ' + at.y + 'px)';
  el.dataset.side = at.flipped ? 'below' : 'above';
  el.hidden = false;
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

// ------------------------------------------------- landscape handle (#1601)

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
  wireKeyboard();
  wireHardwareKeyboard();
  wirePasteSheet();
  wireQuality();
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
