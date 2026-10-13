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
 * picture.
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
  // Wheel units per CSS px a two-finger drag travels (120 = one notch).
  wheelPerPx: 4,
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

/**
 * What a typed character becomes while sticky modifiers are held: a key
 * combo for a letter or digit (Ctrl + c), or null when it can only go out
 * as text.
 */
export function comboKey(ch) {
  return /^[a-z0-9]$/i.test(ch) ? ch.toLowerCase() : null;
}
