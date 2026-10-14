/* Screen control's gesture maths (#1521), as plain functions of numbers.
 *
 * screen-view.js owns the pointers and the socket; the decisions live here so
 * plain Node can pin them (tests/js/screen_gestures.test.mjs): Playwright's
 * touch emulation taps, but it cannot judge a long-press, a two-finger
 * scroll or the letterbox around a zoomed frame.
 *
 * Coordinates sent to the PC are normalised 0-1 within the frame (= the
 * monitor being shown). The frame is drawn `object-fit: contain` inside the
 * stage, then the view's own zoom (translate x/y, then scale) moves it, so a
 * stage point is un-zoomed first and then read against the letterboxed
 * picture. stagePoint goes the other way, for the PC's cursor. Trackpad
 * mode (#1599) moves its own cursor by how far the finger travels
 * (padStep), and pans a zoomed picture to keep it in view (followPan).
 * Two- and three-finger taps (#1600) are decided by isMultiFingerTap.
 */

export const GESTURE = {
  // A press that moves less than this (CSS px) is still a tap or long-press.
  tapSlop: 10,
  // Held this long without moving: a right click (ms).
  longPressMs: 500,
  // A second tap this soon and this close to the first is a double click.
  doubleTapMs: 350,
  doubleTapSlop: 30,
  // Two fingers: the distance between them changing by this much first makes
  // the gesture a pinch; their midpoint moving this much first, a scroll.
  pinchSlop: 16,
  scrollSlop: 12,
  // Two or three fingers that land and all lift again within this long (ms)
  // without moving past their slop are a tap: two right-click, three raise
  // the keyboard (#1600). The trackpad's two-finger tap had this window
  // before (#1599); it is the long-press time, so a rest is not a tap.
  multiTapMs: 500,
  // Wheel units per CSS px a two-finger drag travels (120 = one notch).
  wheelPerPx: 4,
  // Trackpad (#1599): the cursor moves this many CSS px of the picture per
  // px the finger travels, from padGainSlow for a finger at rest up to
  // padGainFast at padFastSpeed (px/ms) and beyond: slow is precise, a
  // flick crosses the screen.
  padGainSlow: 0.8,
  padGainFast: 2.5,
  padFastSpeed: 1.5,
  // A trackpad tap's click waits this long (ms) for a second touch, which
  // makes it a double click, or, held and moved, a drag instead.
  padTapWaitMs: 250,
  // Zoomed in, the cursor stays at least this far (CSS px) inside the stage.
  padFollowMargin: 48,
  // After the last input sent, the server's cursor messages still describe
  // the cursor on its way there for about this long (ms): the locally moved
  // cursor is drawn until then.
  padLeadMs: 400,
};

/**
 * Where a stage point falls on the frame, as {x, y} in 0-1, or null when it
 * is on the letterbox outside the picture. `clamp` pins an outside point to
 * the nearest edge instead (a drag that wanders off the picture).
 *
 * @param {number} qx  stage-local x (CSS px)
 * @param {number} qy  stage-local y
 * @param {{scale:number, x:number, y:number}} zoom  the view's transform
 * @param {number} stageW  stage size
 * @param {number} stageH
 * @param {number} natW  the frame's natural size
 * @param {number} natH
 * @param {boolean} [clamp]
 */
export function frameFraction(qx, qy, zoom, stageW, stageH, natW, natH, clamp) {
  if (!(stageW > 0 && stageH > 0 && natW > 0 && natH > 0)) return null;
  // Undo the zoom: the transform is translate(x, y) scale(s) about 0,0.
  const px = (qx - zoom.x) / zoom.scale;
  const py = (qy - zoom.y) / zoom.scale;
  // object-fit: contain.
  const fit = Math.min(stageW / natW, stageH / natH);
  const drawnW = natW * fit;
  const drawnH = natH * fit;
  let x = (px - (stageW - drawnW) / 2) / drawnW;
  let y = (py - (stageH - drawnH) / 2) / drawnH;
  if (clamp) {
    x = Math.min(1, Math.max(0, x));
    y = Math.min(1, Math.max(0, y));
  } else if (x < 0 || x > 1 || y < 0 || y > 1) {
    return null;
  }
  return { x: round4(x), y: round4(y) };
}

/**
 * The inverse of frameFraction: where the 0-1 frame point (fx, fy) sits on
 * the stage, as {x, y} in stage-local CSS px, or null when it can't be drawn
 * (no frame yet, a point off the frame, or zoomed and panned off the stage).
 * The PC's cursor (#1598) is drawn here, outside the zoom transform, so it
 * keeps its size while the picture zooms.
 */
export function stagePoint(fx, fy, zoom, stageW, stageH, natW, natH) {
  if (!(stageW > 0 && stageH > 0 && natW > 0 && natH > 0)) return null;
  if (!(fx >= 0 && fx <= 1 && fy >= 0 && fy <= 1)) return null;
  const fit = Math.min(stageW / natW, stageH / natH);
  const drawnW = natW * fit;
  const drawnH = natH * fit;
  const x = ((stageW - drawnW) / 2 + fx * drawnW) * zoom.scale + zoom.x;
  const y = ((stageH - drawnH) / 2 + fy * drawnH) * zoom.scale + zoom.y;
  if (x < 0 || x > stageW || y < 0 || y > stageH) return null;
  return { x: x, y: y };
}

function round4(v) { return Math.round(v * 10000) / 10000; }

export function distance(a, b) {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

/** A single press: still a tap/long-press, or now a drag. */
export function movedPastTap(start, now) {
  return distance(start, now) > GESTURE.tapSlop;
}

/** Whether this tap completes a double-tap with the previous one. */
export function isDoubleTap(prev, tap) {
  return !!prev && tap.t - prev.t <= GESTURE.doubleTapMs &&
    distance(prev, tap) <= GESTURE.doubleTapSlop;
}

/**
 * Two fingers: 'pinch', 'scroll', or null while still undecided.
 * `start`/`now` are {dist, mid:{x, y}}.
 */
export function classifyTwoFinger(start, now) {
  if (Math.abs(now.dist - start.dist) > GESTURE.pinchSlop) return 'pinch';
  if (distance(start.mid, now.mid) > GESTURE.scrollSlop) return 'scroll';
  return null;
}

/** Whether any finger has travelled past the tap slop since it landed.
 * `starts` and `nows` are matching lists of {x, y}. */
export function anyMovedPastTap(starts, nows) {
  return starts.some(function (s, i) { return !!nows[i] && movedPastTap(s, nows[i]); });
}

/**
 * Two or three fingers that were down for `heldMs`, having `moved` or not: a
 * tap when they never moved past their slop and did not stay down as long
 * as a long-press. One decision for the two-finger right-click in both
 * Control styles and for the three-finger keyboard tap (#1600). For two
 * fingers `moved` means "classified as a pinch or a scroll".
 */
export function isMultiFingerTap(moved, heldMs) {
  return !moved && heldMs >= 0 && heldMs < GESTURE.multiTapMs;
}

/**
 * Wheel deltas for a two-finger drag of (dx, dy) CSS px, natural direction:
 * fingers moving down show what is above, as on the phone itself. Windows'
 * vertical wheel is positive away from the user (content moves down) and its
 * horizontal wheel positive to the right.
 */
export function wheelFromDrag(dx, dy) {
  return {
    dx: Math.round(-dx * GESTURE.wheelPerPx) || 0,
    dy: Math.round(dy * GESTURE.wheelPerPx) || 0,
  };
}

/** A desktop wheel event's deltas, in Windows wheel units. */
export function wheelFromWheelEvent(deltaX, deltaY, deltaMode) {
  // Pixels: a mouse notch is ~100px; lines: ~3 per notch; pages: a lot.
  const unit = deltaMode === 1 ? 40 : deltaMode === 2 ? 1200 : 1.2;
  return { dx: Math.round(deltaX * unit) || 0, dy: Math.round(-deltaY * unit) || 0 };
}

/** Trackpad: the gain for a finger moving at `speed` CSS px/ms. */
export function padGain(speed) {
  const t = Math.min(1, Math.max(0, speed / GESTURE.padFastSpeed)) || 0;
  return GESTURE.padGainSlow + (GESTURE.padGainFast - GESTURE.padGainSlow) * t;
}

/**
 * Trackpad (#1599): the cursor, 0-1 in the frame, after the finger moved
 * (dx, dy) CSS px in `dtMs`. The step is in px of the picture as drawn
 * (drawnW x drawnH unzoomed), divided by the zoom, so the cursor crosses
 * the stage at the same speed zoomed or not. It stops at the frame's edge.
 */
export function padStep(cursor, dx, dy, dtMs, drawnW, drawnH, scale) {
  if (!(drawnW > 0 && drawnH > 0)) return cursor;
  // Coalesced pointer events can arrive a few ms apart: no speed spikes.
  const speed = Math.hypot(dx, dy) / Math.max(dtMs || 0, 8);
  const gain = padGain(speed) / (scale > 0 ? scale : 1);
  return {
    x: round4(Math.min(1, Math.max(0, cursor.x + dx * gain / drawnW))),
    y: round4(Math.min(1, Math.max(0, cursor.y + dy * gain / drawnH))),
  };
}

/**
 * Zoomed in, the pan that keeps the trackpad cursor in view: given the
 * cursor's unzoomed stage point (bx, by), the zoom's x/y moved just enough
 * that it sits `margin` px (at most a quarter of the stage) inside the
 * stage, or unchanged when it already does. The caller then clamps the pan
 * so the picture still covers the stage, which lets the cursor reach the
 * stage's edge at the picture's own edge.
 */
export function followPan(zoom, bx, by, stageW, stageH, margin) {
  const axis = function (base, offset, size) {
    const m = Math.min(margin, size / 4);
    const at = base * zoom.scale + offset;
    if (at < m) return m - base * zoom.scale;
    if (at > size - m) return size - m - base * zoom.scale;
    return offset;
  };
  return { x: axis(bx, zoom.x, stageW), y: axis(by, zoom.y, stageH) };
}

/**
 * The Control style a device remembers (#1599): what the storage key held,
 * or direct touch for a first use, a blocked storage (null) or junk.
 */
export function controlStyle(stored) {
  return stored === 'trackpad' ? 'trackpad' : 'touch';
}

/**
 * What a typed character becomes while sticky modifiers are held: a key
 * combo for a letter or digit (Ctrl + c), or null when it can only go out
 * as text.
 */
export function comboKey(ch) {
  return /^[a-z0-9]$/i.test(ch) ? ch.toLowerCase() : null;
}
