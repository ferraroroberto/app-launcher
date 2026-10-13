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
 * Enter, and Ctrl / Alt / Shift / Win, sticky for one key). sendInput() is
 * the one way out, and it sends nothing in View mode or while the picture
 * is not live: input aimed at a stale frame would land somewhere else.
 * Coordinates are 0-1 within the frame (screen-gestures.js maps them); the
 * server turns them into PC pixels.
 */

import { isDesktopClient, readToken, AuthRequiredError } from './api.js';
import { clearTerminalToken, ensureTerminalToken, terminalJsonApi } from './webauthn.js';
import { setAsyncState, staleMessage } from './async-state.js';
import {
  GESTURE, classifyTwoFinger, comboKey, distance, frameFraction, isDoubleTap,
  movedPastTap, wheelFromDrag, wheelFromWheelEvent,
} from './screen-gestures.js';

const MAX_ZOOM = 5;
// The widest frame worth asking for: the phone's long side in device pixels,
// capped (the server never sends more than the monitor's own width).
const MAX_FRAME_WIDTH = 1920;
const POPUP_NAME = 'launcher-screen';
const POPUP_FEATURES = 'popup,width=1280,height=760';

const view = {
  open: false,
  standalone: false,   // this window is the pop-out: closing closes it
  monitors: [],
  monitorId: null,
  ws: null,
  frameUrl: '',
  lastFrameAt: 0,
  zoom: { scale: 1, x: 0, y: 0 },
  mode: 'view',        // 'view' | 'control'
  live: false,         // a frame is drawn and the desktop is readable
  mods: new Set(),     // sticky modifiers for the next key
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
  setMode('view');
  resetZoom();
  await start();
}

async function start() {
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
    const primary = view.monitors.find(function (m) { return m.primary; });
    view.monitorId = (primary || view.monitors[0]).id;
  }
  renderPicker();
  await connect();
}

function showRefusal(message, glyph) {
  setState('error', { message: message, glyph: glyph || 'lock', retry: start });
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
  const bearer = readToken();
  if (bearer) params.set('token', bearer);
  if (terminalToken) params.set('tt', terminalToken);
  return proto + '//' + location.host + '/api/screen/ws?' + params.toString();
}

async function connect() {
  closeSocket();
  let tt;
  try {
    tt = await ensureTerminalToken();
  } catch (exc) {
    showRefusal('Passkey unlock failed.');
    return;
  }
  if (!view.open) return;
  const ws = new WebSocket(socketUrl(tt));
  ws.binaryType = 'blob';
  view.ws = ws;
  ws.onmessage = function (event) {
    if (ws !== view.ws) return;
    if (typeof event.data === 'string') {
      onStatus(event.data);
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

function onStatus(text) {
  let msg = null;
  try { msg = JSON.parse(text); } catch (_) { return; }
  if (!msg || msg.type !== 'status') return;
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
  const ack = function () {
    if (ws === view.ws && ws.readyState === WebSocket.OPEN) ws.send('ack');
  };
  img.onload = function () {
    const previous = view.frameUrl;
    view.frameUrl = url;
    view.lastFrameAt = Date.now();
    if (previous) URL.revokeObjectURL(previous);
    img.hidden = false;
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
  if (!view.open) return;
  const reason = event && event.reason ? event.reason : '';
  if (event.code === 4403) {
    showRefusal('The screen is Tailscale-only. Open the launcher over your Tailscale URL.');
  } else if (event.code === 4401) {
    if (/passkey/i.test(reason)) clearTerminalToken();
    showRefusal(/passkey/i.test(reason) ? 'Passkey unlock required.' : 'Sign in again to view the screen.');
  } else if (event.code === 4404) {
    showRefusal('That display is gone. Retry to pick again.', 'monitor');
  } else {
    // The last frame stays behind the message, so it is still readable.
    setState('error', { message: 'Disconnected from the screen.', glyph: 'monitor', retry: start });
  }
}

function closeSocket() {
  const ws = view.ws;
  view.ws = null;
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
}

function hideView() {
  view.open = false;
  setMode('view');
  closeSocket();
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

// ---------------------------------------------------------------- control

// The one way input leaves the view: Control mode, an open socket, and a
// live picture, or nothing is sent.
function sendInput(msg) {
  const ws = view.ws;
  if (view.mode !== 'control' || !view.live || !ws || ws.readyState !== WebSocket.OPEN) {
    return false;
  }
  ws.send(JSON.stringify(msg));
  return true;
}

function setLive(live) {
  view.live = live;
  const keys = $('screenKeys');
  if (keys) {
    keys.querySelectorAll('.key-btn').forEach(function (btn) { btn.disabled = !live; });
  }
}

function setMode(mode) {
  if (mode !== 'control') {
    // Lift a drag in progress while input can still go out.
    cancelGesture();
    setKeysOpen(false);
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
//   {kind: 'two', start, lastMid, scale0, cls}  two fingers
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

function cancelGesture() {
  if (!gesture) return;
  if (gesture.timer) clearTimeout(gesture.timer);
  if (gesture.kind === 'drag') {
    flushMove();
    const at = framePoint(gesture.last, true);
    if (at) sendInput({ type: 'up', button: 'left', x: at.x, y: at.y });
  }
  gesture = null;
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
      }, GESTURE.longPressMs),
    };
  } else if (pointers.size === 2) {
    cancelGesture();
    const p = twoFingerState(pointers);
    gesture = { kind: 'two', start: p, lastMid: p.mid, scale0: view.zoom.scale, cls: null };
  }
}

function controlMove(pointers, pt) {
  if (!gesture) return;
  if (gesture.kind === 'press') {
    if (!movedPastTap(gesture.start, pt)) return;
    clearTimeout(gesture.timer);
    const at = framePoint(gesture.start);
    if (!at) { gesture = { kind: 'spent' }; return; }
    sendInput({ type: 'down', button: 'left', x: at.x, y: at.y });
    gesture = { kind: 'drag', last: pt };
  }
  if (gesture.kind === 'drag') {
    gesture.last = pt;
    const at = framePoint(pt, true);
    if (at) queueMove({ type: 'move', x: at.x, y: at.y });
    return;
  }
  if (gesture.kind === 'two' && pointers.size === 2) {
    const now = twoFingerState(pointers);
    if (!gesture.cls) gesture.cls = classifyTwoFinger(gesture.start, now);
    if (gesture.cls === 'pinch') {
      pinchTo(gesture.scale0, gesture.start, now, gesture.lastMid);
    } else if (gesture.cls === 'scroll') {
      const at = framePoint(now.mid, true);
      const wheel = wheelFromDrag(now.mid.x - gesture.lastMid.x, now.mid.y - gesture.lastMid.y);
      if (at && (wheel.dx || wheel.dy)) {
        queueMove({ type: 'scroll', x: at.x, y: at.y, dx: wheel.dx, dy: wheel.dy });
      }
    }
    gesture.lastMid = now.mid;
  }
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
  } else if (gesture.kind === 'two') {
    // One finger lifting ends the two-finger gesture; the other must not
    // start a drag.
    gesture = { kind: 'spent' };
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
  if (!open) clearMods();
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
    kb.addEventListener('click', function () {
      setKeysOpen(!keys || keys.hidden);
    });
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
      if (btn.dataset.key) sendKey(btn.dataset.key);
    });
  }
  if (!input) return;
  // beforeinput, not keydown: a phone keyboard's IME reports keyCode 229 for
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

// ----------------------------------------------------------- stage wiring

function wireStage(stage) {
  const pointers = new Map();
  let pinch = null;

  function local(e) {
    const r = stage.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }

  stage.addEventListener('pointerdown', function (e) {
    if (e.target.closest('button')) return;
    try { stage.setPointerCapture(e.pointerId); } catch (_) { /* synthetic */ }
    const pt = local(e);
    pointers.set(e.pointerId, pt);
    if (view.mode === 'control') {
      controlDown(pointers, e, pt);
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
      controlMove(pointers, now);
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
      const at = framePoint(q, true);
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
  if (stage) wireStage(stage);
  const modePicker = $('screenMode');
  if (modePicker) {
    modePicker.addEventListener('click', function (e) {
      const btn = e.target.closest('.segmented-item');
      if (btn && btn.dataset.mode && btn.dataset.mode !== view.mode) setMode(btn.dataset.mode);
    });
  }
  wireKeyboard();
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
    else if (!view.ws && view.monitorId !== null) connect();
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
