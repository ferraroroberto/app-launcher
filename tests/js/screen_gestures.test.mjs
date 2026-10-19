// Unit pin for Screen control's gesture maths (#1521): screen-gestures.js
// keeps the decisions as plain functions, because Playwright's touch
// emulation only taps. The letterbox + zoom mapping, the tap / long-press /
// double-tap / pinch / scroll thresholds and the wheel directions are pinned
// here under plain Node, as are the PC cursor's placement (stagePoint, the
// inverse of the frame mapping) and its shape fallback (#1598), and the
// trackpad's cursor step, zoom follow and remembered style (#1599). Invoked by
// tests/test_screen_gestures.py.

import assert from 'node:assert/strict';

const {
  GESTURE, frameFraction, stagePoint, movedPastTap, isDoubleTap, classifyTwoFinger,
  wheelFromDrag, wheelFromWheelEvent, comboKey, padGain, padStep, followPan, controlStyle,
  anyMovedPastTap, isMultiFingerTap,
} = await import('../../app/webapp/static/screen-gestures.js');
const { CURSOR_SHAPES, cursorShape, cursorSvg } = await import(
  '../../app/webapp/static/screen-cursor.js');

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

// The cursor's spot (#1598): stagePoint is frameFraction's inverse, with
// and without zoom, letterbox included.
const close = (a, b) => Math.abs(a - b) < 1e-6;
const P = stagePoint(0.25, 0.75, NO_ZOOM, W, H, NW, NH);
assert.ok(close(P.x, W / 4) && close(P.y, top + drawnH * 0.75), JSON.stringify(P));
assert.deepEqual(frameFraction(P.x, P.y, NO_ZOOM, W, H, NW, NH), { x: 0.25, y: 0.75 });
const Pz = stagePoint(0.4, 0.6, Z2, W, H, NW, NH);
assert.deepEqual(frameFraction(Pz.x, Pz.y, Z2, W, H, NW, NH), { x: 0.4, y: 0.6 });
// Zoomed 2x about the centre: the frame's centre stays put, so the cursor
// moves twice as far from it as the picture point it marks would unzoomed.
const C = stagePoint(0.5, 0.5, Z2, W, H, NW, NH);
assert.ok(close(C.x, W / 2) && close(C.y, H / 2), JSON.stringify(C));
// Pillarboxed: the frame's left edge is past the bar, not at the stage's.
assert.deepEqual(stagePoint(0, 0.5, NO_ZOOM, 1000, 100, 160, 90),
  { x: (1000 - 160 * 100 / 90) / 2, y: 50 });
// Panned off the stage while zoomed: not drawn.
assert.equal(stagePoint(0.1, 0.5, Z2, W, H, NW, NH), null);
// Off the frame, garbage, or no frame yet: not drawn.
assert.equal(stagePoint(1.2, 0.5, NO_ZOOM, W, H, NW, NH), null);
assert.equal(stagePoint(-0.01, 0.5, NO_ZOOM, W, H, NW, NH), null);
assert.equal(stagePoint(NaN, 0.5, NO_ZOOM, W, H, NW, NH), null);
assert.equal(stagePoint(undefined, 0.5, NO_ZOOM, W, H, NW, NH), null);
assert.equal(stagePoint(0.5, 0.5, NO_ZOOM, W, H, 0, 0), null);

// Every shape the server sends has a drawing; anything else is the arrow.
for (const name of ['arrow', 'text', 'hand', 'busy', 'resize-ns', 'resize-ew',
  'resize-nwse', 'resize-nesw', 'move', 'not-allowed']) {
  assert.equal(cursorShape(name), name);
  assert.ok(CURSOR_SHAPES[name].hot.length === 2, name);
}
assert.equal(cursorShape('crosshair'), 'arrow');
assert.equal(cursorShape('__proto__'), 'arrow');
assert.equal(cursorShape(undefined), 'arrow');
assert.equal(cursorSvg('nope'), cursorSvg('arrow'));
// The hotspot lands on the anchor: the arrow's tip, the I-beam's middle.
assert.match(cursorSvg('arrow'), /left:-5px;top:-3px/);
assert.match(cursorSvg('text'), /left:-12px;top:-12px/);

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

// Trackpad (#1599). The gain ramps from slow to fast with finger speed and
// stops there.
assert.equal(padGain(0), GESTURE.padGainSlow);
assert.equal(padGain(GESTURE.padFastSpeed), GESTURE.padGainFast);
assert.equal(padGain(GESTURE.padFastSpeed * 10), GESTURE.padGainFast);
assert.ok(close(padGain(GESTURE.padFastSpeed / 2),
  (GESTURE.padGainSlow + GESTURE.padGainFast) / 2));
assert.equal(padGain(NaN), GESTURE.padGainSlow);

// A slow 10px finger move over 100ms on the 390px-wide picture moves the
// cursor 10 * gain px of it; the y axis maps against the drawn height.
const mid = { x: 0.5, y: 0.5 };
const slowGain = padGain(10 / 100);
let c1 = padStep(mid, 10, 0, 100, W, drawnH, 1);
assert.ok(Math.abs(c1.x - (0.5 + 10 * slowGain / W)) < 1e-4, JSON.stringify(c1));
assert.equal(c1.y, 0.5);
c1 = padStep(mid, 0, -10, 100, W, drawnH, 1);
assert.ok(Math.abs(c1.y - (0.5 - 10 * slowGain / drawnH)) < 1e-4, JSON.stringify(c1));
// Faster is further: the same 10px in 5ms (clamped to 8ms) travels more.
assert.ok(padStep(mid, 10, 0, 5, W, drawnH, 1).x > padStep(mid, 10, 0, 100, W, drawnH, 1).x);
// Zoomed 2x, the cursor moves half as far in the frame: the same on screen.
const z1 = padStep(mid, 10, 0, 100, W, drawnH, 1).x - 0.5;
const z2 = padStep(mid, 10, 0, 100, W, drawnH, 2).x - 0.5;
assert.ok(Math.abs(z2 - z1 / 2) < 1e-4, `${z1} ${z2}`);
// It stops at the frame's edges, and does nothing without a picture.
assert.deepEqual(padStep({ x: 0.99, y: 0.01 }, 500, -500, 10, W, drawnH, 1), { x: 1, y: 0 });
assert.deepEqual(padStep(mid, 10, 10, 10, 0, 0, 1), mid);

// Follow-pan: zoomed 2x about the centre (Z2), a cursor near the stage's
// middle needs no pan.
assert.deepEqual(followPan(Z2, W / 2, H / 2, W, H, 48), { x: Z2.x, y: Z2.y });
// The cursor's unzoomed point 300px across sits at 300 * 2 - 195 = 405, off
// the 390px stage: pan left until it is 48px inside the right edge.
let pan = followPan(Z2, 300, H / 2, W, H, 48);
assert.ok(close(pan.x, W - 48 - 600) && close(pan.y, Z2.y), JSON.stringify(pan));
// Past the left / top edges: pan the other way.
pan = followPan(Z2, 50, 100, W, H, 48);
assert.ok(close(pan.x, 48 - 100) && close(pan.y, 48 - 200), JSON.stringify(pan));
// On a tiny stage the margin is at most a quarter of it.
pan = followPan({ scale: 2, x: 0, y: 0 }, 90, 10, 100, 100, 48);
assert.ok(close(pan.x, 75 - 180), JSON.stringify(pan));

// Two- and three-finger taps (#1600): never moved and lifted before a
// long-press would have fired. Any movement past the slop, or a rest as long
// as a long-press, is not a tap.
assert.equal(GESTURE.multiTapMs, GESTURE.longPressMs);
assert.equal(isMultiFingerTap(false, 0), true);
assert.equal(isMultiFingerTap(false, GESTURE.multiTapMs - 1), true);
assert.equal(isMultiFingerTap(false, GESTURE.multiTapMs), false);
assert.equal(isMultiFingerTap(false, GESTURE.multiTapMs + 400), false);
assert.equal(isMultiFingerTap(true, 50), false);
assert.equal(isMultiFingerTap(false, -1), false);
// Two fingers: classifyTwoFinger's null (undecided) is what "never moved" is.
const twoStart = { dist: 100, mid: { x: 100, y: 100 } };
assert.equal(isMultiFingerTap(!!classifyTwoFinger(twoStart, { dist: 104, mid: { x: 103, y: 102 } }), 120), true);
assert.equal(isMultiFingerTap(!!classifyTwoFinger(twoStart, { dist: 100, mid: { x: 100, y: 130 } }), 120), false);
assert.equal(isMultiFingerTap(!!classifyTwoFinger(twoStart, { dist: 140, mid: { x: 100, y: 100 } }), 120), false);
// Three fingers: any one past the tap slop spoils it; a wobble inside does not.
const three = [{ x: 50, y: 50 }, { x: 100, y: 50 }, { x: 150, y: 50 }];
assert.equal(anyMovedPastTap(three, three.map((p) => ({ x: p.x + 3, y: p.y - 4 }))), false);
assert.equal(anyMovedPastTap(three, three.map((p) => ({ x: p.x + GESTURE.tapSlop, y: p.y }))), false);
assert.equal(anyMovedPastTap(three, [three[0], three[1], { x: 150, y: 50 + GESTURE.tapSlop + 1 }]), true);
// A finger that has gone from the list is not a move.
assert.equal(anyMovedPastTap(three, [three[0], three[1]]), false);

// The remembered Control style: trackpad only when stored as such; a
// first use, a blocked storage or junk is direct touch.
assert.equal(controlStyle('trackpad'), 'trackpad');
assert.equal(controlStyle('touch'), 'touch');
assert.equal(controlStyle(null), 'touch');
assert.equal(controlStyle('Trackpad'), 'touch');

// Sticky modifiers turn letters, digits and every printable ASCII character
// into a combo (#1602); the PC resolves the character on its own layout.
assert.equal(comboKey('c'), 'c');
assert.equal(comboKey('A'), 'a');
assert.equal(comboKey('7'), '7');
for (const ch of ['/', ',', ';', '.', '-', '=', '[', ']', '\\', "'", '`', '?', '!', '@', '~', '|', '"']) {
  assert.equal(comboKey(ch), ch, ch);
}
assert.equal(comboKey(' '), 'space');
let printable = 0;
for (let code = 0x21; code <= 0x7e; code++) {
  assert.notEqual(comboKey(String.fromCharCode(code)), null, String.fromCharCode(code));
  printable++;
}
assert.equal(printable, 94);
// Only text can carry these.
assert.equal(comboKey('ñ'), null);
assert.equal(comboKey('€'), null);
assert.equal(comboKey('😀'), null);
assert.equal(comboKey('ab'), null);
assert.equal(comboKey(String.fromCharCode(10)), null);
assert.equal(comboKey(String.fromCharCode(9)), null);
assert.equal(comboKey(String.fromCharCode(0x7f)), null);
assert.equal(comboKey(''), null);
assert.equal(comboKey(undefined), null);


// Hardware keyboard (#1723): which key presses become a `key` message.
{
  const { hardwareKey, allowKeyRepeat, KEY_REPEAT } = await import(
    '../../app/webapp/static/screen-gestures.js');
  const press = (key, extra) => hardwareKey({ key, ...(extra || {}) });

  // Named keys, with and without a modifier.
  for (const [key, named] of [
    ['ArrowUp', 'up'], ['ArrowDown', 'down'], ['ArrowLeft', 'left'], ['ArrowRight', 'right'],
    ['Escape', 'esc'], ['Tab', 'tab'], ['Enter', 'enter'], ['Backspace', 'backspace'],
    ['Delete', 'delete'], ['Home', 'home'], ['End', 'end'], ['PageUp', 'pageup'],
    ['PageDown', 'pagedown'], ['F1', 'f1'], ['F5', 'f5'], ['F12', 'f12'],
  ]) {
    assert.deepEqual(press(key), { key: named, mods: [] }, key);
  }
  assert.deepEqual(press('Tab', { shiftKey: true }), { key: 'tab', mods: ['shift'] });
  assert.equal(press('F13'), null);

  // A modifier combo: one key message, mods in the server's order.
  assert.deepEqual(press('c', { ctrlKey: true }), { key: 'c', mods: ['ctrl'] });
  assert.deepEqual(press('C', { ctrlKey: true, shiftKey: true }), { key: 'c', mods: ['ctrl', 'shift'] });
  assert.deepEqual(press('4', { altKey: true }), { key: '4', mods: ['alt'] });
  assert.deepEqual(press('/', { ctrlKey: true }), { key: '/', mods: ['ctrl'] });
  assert.deepEqual(press(' ', { ctrlKey: true }), { key: 'space', mods: ['ctrl'] });
  assert.deepEqual(press('ArrowLeft', { ctrlKey: true, altKey: true }), { key: 'left', mods: ['ctrl', 'alt'] });
  // Alt on a Mac turns the character into a symbol: the physical key decides.
  assert.deepEqual(press('å', { altKey: true, code: 'KeyA' }), { key: 'a', mods: ['alt'] });
  assert.equal(press('å', { altKey: true }), null);

  // Cmd is Ctrl, never Win, and Cmd with Ctrl is one Ctrl.
  assert.deepEqual(press('c', { metaKey: true }), { key: 'c', mods: ['ctrl'] });
  assert.deepEqual(press('v', { metaKey: true, ctrlKey: true }), { key: 'v', mods: ['ctrl'] });
  assert.ok(!press('c', { metaKey: true }).mods.includes('win'));
  assert.deepEqual(press('ArrowRight', { metaKey: true }), { key: 'right', mods: ['ctrl'] });

  // Plain text stays with beforeinput, Shift included; a bare modifier is no key.
  assert.equal(press('a'), null);
  assert.equal(press('A', { shiftKey: true }), null);
  assert.equal(press('ñ'), null);
  assert.equal(press(' '), null);
  assert.equal(press('Shift', { shiftKey: true }), null);
  assert.equal(press('Control', { ctrlKey: true }), null);
  assert.equal(press('Meta', { metaKey: true }), null);
  assert.equal(press('Dead', { altKey: true }), null);

  // An IME composition is never a key press.
  assert.equal(press('Enter', { isComposing: true }), null);
  assert.equal(press('a', { keyCode: 229, ctrlKey: true }), null);

  // AltGr (Ctrl+Alt on Windows) types a character: it stays text, even with a named key's mods dropped.
  const altGr = { ctrlKey: true, altKey: true, getModifierState: (m) => m === 'AltGraph' };
  assert.equal(press('@', altGr), null);
  assert.deepEqual(press('ArrowLeft', altGr), { key: 'left', mods: [] });

  // Held keys: spaced out and bounded.
  assert.ok(!allowKeyRepeat(1, KEY_REPEAT.minGapMs - 1));
  assert.ok(allowKeyRepeat(1, KEY_REPEAT.minGapMs));
  assert.ok(allowKeyRepeat(KEY_REPEAT.maxRepeats, 100));
  assert.ok(!allowKeyRepeat(KEY_REPEAT.maxRepeats + 1, 100));
}

console.log('OK');
