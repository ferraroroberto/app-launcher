// Unit pin for Screen control's gesture maths (#1521): screen-gestures.js
// keeps the decisions as plain functions, because Playwright's touch
// emulation only taps. The letterbox + zoom mapping, the tap / long-press /
// double-tap / pinch / scroll thresholds and the wheel directions are pinned
// here under plain Node. Invoked by tests/test_screen_gestures.py.

import assert from 'node:assert/strict';

const {
  GESTURE, frameFraction, movedPastTap, isDoubleTap, classifyTwoFinger,
  wheelFromDrag, wheelFromWheelEvent, comboKey,
} = await import('../../app/webapp/static/screen-gestures.js');

const NO_ZOOM = { scale: 1, x: 0, y: 0 };

// A 16:9 frame in a 390x600 phone stage: full width, letterboxed top and
// bottom. Drawn 390 x 219.375, starting at y = 190.3125.
const W = 390, H = 600, NW = 1920, NH = 1080;
const top = (H - W * NH / NW) / 2;
const drawnH = W * NH / NW;

assert.deepEqual(frameFraction(0, top, NO_ZOOM, W, H, NW, NH), { x: 0, y: 0 });
assert.deepEqual(frameFraction(W, top + drawnH, NO_ZOOM, W, H, NW, NH), { x: 1, y: 1 });
assert.deepEqual(frameFraction(W / 2, H / 2, NO_ZOOM, W, H, NW, NH), { x: 0.5, y: 0.5 });
assert.deepEqual(frameFraction(W / 4, top + drawnH * 0.75, NO_ZOOM, W, H, NW, NH), { x: 0.25, y: 0.75 });
// The letterbox is not the picture: a tap there clicks nothing.
assert.equal(frameFraction(100, top - 5, NO_ZOOM, W, H, NW, NH), null);
assert.equal(frameFraction(100, top + drawnH + 5, NO_ZOOM, W, H, NW, NH), null);
// ...but a drag that wanders there pins to the edge.
assert.deepEqual(frameFraction(-20, top - 50, NO_ZOOM, W, H, NW, NH, true), { x: 0, y: 0 });
assert.deepEqual(frameFraction(W + 9, H, NO_ZOOM, W, H, NW, NH, true), { x: 1, y: 1 });

// A pillarboxed frame (a wide stage): the bars are left and right.
assert.equal(frameFraction(10, 50, NO_ZOOM, 1000, 100, 160, 90), null);
assert.deepEqual(frameFraction(500, 50, NO_ZOOM, 1000, 100, 160, 90), { x: 0.5, y: 0.5 });

// Zoomed 2x about the stage centre: translate(-W/2, -H/2) scale(2). The
// stage centre still shows the frame centre; the stage's top-left now shows
// the point a quarter of the way in.
const Z2 = { scale: 2, x: -W / 2, y: -H / 2 };
assert.deepEqual(frameFraction(W / 2, H / 2, Z2, W, H, NW, NH), { x: 0.5, y: 0.5 });
assert.deepEqual(frameFraction(0, H / 2, Z2, W, H, NW, NH), { x: 0.25, y: 0.5 });

// No frame drawn yet: nothing to map.
assert.equal(frameFraction(10, 10, NO_ZOOM, W, H, 0, 0), null);
assert.equal(frameFraction(10, 10, NO_ZOOM, 0, H, NW, NH), null);

// Tap slop.
assert.equal(GESTURE.tapSlop, 10);
assert.equal(movedPastTap({ x: 0, y: 0 }, { x: 6, y: 8 }), false);   // 10px: still a tap
assert.equal(movedPastTap({ x: 0, y: 0 }, { x: 6, y: 9 }), true);
assert.equal(GESTURE.longPressMs, 500);

// Double-tap: soon and close.
const first = { t: 1000, x: 100, y: 100 };
assert.equal(isDoubleTap(null, first), false);
assert.equal(isDoubleTap(first, { t: 1300, x: 110, y: 120 }), true);
assert.equal(isDoubleTap(first, { t: 1400, x: 100, y: 100 }), false);  // too late
assert.equal(isDoubleTap(first, { t: 1100, x: 140, y: 100 }), false);  // too far

// Two fingers: whichever slop is crossed first decides.
const s = { dist: 100, mid: { x: 200, y: 300 } };
assert.equal(classifyTwoFinger(s, { dist: 110, mid: { x: 205, y: 305 } }), null);
assert.equal(classifyTwoFinger(s, { dist: 130, mid: { x: 200, y: 300 } }), 'pinch');
assert.equal(classifyTwoFinger(s, { dist: 70, mid: { x: 200, y: 300 } }), 'pinch');
assert.equal(classifyTwoFinger(s, { dist: 104, mid: { x: 200, y: 320 } }), 'scroll');

// Natural scrolling: fingers down -> wheel up (positive), fingers right ->
// content right -> horizontal wheel left (negative).
assert.deepEqual(wheelFromDrag(0, 10), { dx: 0, dy: 40 });
assert.deepEqual(wheelFromDrag(0, -10), { dx: 0, dy: -40 });
assert.deepEqual(wheelFromDrag(10, 0), { dx: -40, dy: 0 });
// A desktop wheel notch down (deltaY 100px) is one notch down (-120).
assert.deepEqual(wheelFromWheelEvent(0, 100, 0), { dx: 0, dy: -120 });
assert.deepEqual(wheelFromWheelEvent(0, 3, 1), { dx: 0, dy: -120 });

// Sticky modifiers turn letters and digits into combos, nothing else.
assert.equal(comboKey('c'), 'c');
assert.equal(comboKey('A'), 'a');
assert.equal(comboKey('7'), '7');
assert.equal(comboKey('ñ'), null);
assert.equal(comboKey(' '), null);
assert.equal(comboKey('ab'), null);

console.log('OK');
