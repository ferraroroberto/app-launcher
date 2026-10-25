/* Screen view's picture and touches: zoom and pan, the PC's cursor drawn
 * over the picture, Control's gestures in both styles, and the loupe.
 *
 * Control. The View / Control switch picks what touches do. View
 * (the default on every open, so a stray tap never clicks) only zooms and pans.
 * Control sends input messages on the same socket: tap = click, a second
 * tap soon after = double click, long-press = right click, press-and-drag =
 * drag, two-finger drag = scroll wheel; a two-finger pinch still zooms (and
 * pans) locally. The keyboard toggle raises the phone keyboard through a
 * hidden input and shows the keys bar (Esc, Tab, arrows, Backspace, Del,
 * Enter, and Ctrl / Alt / Shift / Win, sticky for one key; a second page
 * holds F1-F12 and Home / End / PgUp / PgDn). A hardware keyboard's
 * non-text keys and Ctrl / Alt / Cmd shortcuts arrive on keydown. sendInput() is
 * the one way out, and it sends nothing in View mode or while the picture
 * is not live: input aimed at a stale frame would land somewhere else.
 * Coordinates are 0-1 within the frame (screen-gestures.js maps them); the
 * server turns them into PC pixels.
 *
 * Cursor. The picture never contains the PC's mouse cursor, so the
 * server sends it as its own `cursor` messages (0-1 in the frame, a shape
 * name, visible or not) whenever it moves, frame or no frame. placeCursor()
 * draws it over the picture with stagePoint(), outside the zoom transform,
 * so it keeps its size when zoomed; it never takes a touch, and it hides
 * off the picture, off the stage, while the picture is not live, and when
 * the PC hides it.
 *
 * Trackpad. Control has two styles, picked by the Touch / Trackpad
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
 * Loupe. In Touch control the finger covers the spot it is about to
 * click, so while one finger is down (after 150 ms) and while it drags, a round
 * loupe above it shows the local frame under it at 2.5x. It is drawn from the
 * frame already in hand (no network), sits in its own overlay like the cursor,
 * and never takes a touch. Purely visual: no gesture, threshold or click point
 * depends on it. Hidden in View, in Trackpad, with two fingers down and on lift.
 */

import {
  GESTURE, LOUPE, anyMovedPastTap, classifyTwoFinger, distance, followPan, frameFraction, isDoubleTap,
  isMultiFingerTap, loupeLayout, movedPastTap, padStep, stagePoint, wheelFromDrag,
} from './screen-gestures.js';
import { cursorShape, cursorSvg } from './screen-cursor.js';
import { $, view } from './screen-state.js';

const MAX_ZOOM = 5;

// A three-finger tap raises or lowers the keyboard, which screen-keys.js
// owns; wireControl hands it in, so this module never imports the keys back.
let onThreeFingerTap = function () {};

export function wireControl(opts) {
  onThreeFingerTap = opts.onThreeFingerTap;
}

// ---------------------------------------------------------- zoom and pan

export function resetZoom() {
  view.zoom = { scale: 1, x: 0, y: 0 };
  applyZoom();
}

export function applyZoom() {
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
export function zoomAt(scale, qx, qy) {
  const z = view.zoom;
  const next = Math.min(MAX_ZOOM, Math.max(1, scale));
  z.x = qx - (qx - z.x) * (next / z.scale);
  z.y = qy - (qy - z.y) * (next / z.scale);
  z.scale = next;
  applyZoom();
}

// ----------------------------------------------------------------- cursor

export function onCursor(msg) {
  view.cursor = msg;
  syncPad();
  placeCursor();
}

export function isPad() {
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
export function syncPad() {
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
export function placeCursor() {
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
export function canSend() {
  const ws = view.ws;
  return view.mode === 'control' && view.live && !!ws && ws.readyState === WebSocket.OPEN;
}

export function sendInput(msg) {
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

// Where a stage point lands on the frame, 0-1, or null off the picture.
export function framePoint(pt, clamp) {
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
export function queueMove(msg) {
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
export function dropGesture() {
  if (gesture && gesture.timer) clearTimeout(gesture.timer);
  gesture = null;
  lastTap = null;
  pendingMove = null;
  if (padTap) clearTimeout(padTap.timer);
  padTap = null;
  showHeld(false);
  placeLoupe();
}

export function cancelGesture() {
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

export function controlDown(pointers, e, pt) {
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
// keyboard. Both Control styles share this.
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
    onThreeFingerTap();
    return;
  }
  const at = isPad() ? view.pad : framePoint(g.lastMid);
  if (at) sendInput({ type: 'click', button: 'right', x: at.x, y: at.y });
}

export function controlMove(pointers, pt) {
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

export function controlUp(pointers, pt) {
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
// mode. Touching never clicks by itself: a touch that moves only
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

export function padDown(pointers, e, pt) {
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

export function padMove(pointers, pt) {
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

export function padUp(pointers) {
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

export function twoFingerState(pointers) {
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
export function placeLoupe() {
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
