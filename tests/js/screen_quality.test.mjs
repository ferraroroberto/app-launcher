// Unit pin for the Screen view's quality presets and live readout (#1721): the
// preset ids the connect query carries, the remembered choice, and the
// readout's arithmetic over a known sequence of frames. screen-quality.js keeps
// them as plain functions and one small class; the socket and the overlay are
// pinned by tests/e2e/test_screen_quality.py. Invoked by
// tests/test_screen_quality.py.

import assert from 'node:assert/strict';

const {
  PRESETS, PRESET_KEY, DEFAULT_PRESET, presetChoice, presetNote,
  READOUT_WINDOW_MS, READOUT_MIN_SPAN_MS, Readout, readoutLines,
} = await import('../../app/webapp/static/screen-quality.js');

// The ids are the server's table: sharp, balanced, saver, Balanced by default.
assert.deepEqual(PRESETS.map((p) => p.id), ['sharp', 'balanced', 'saver']);
assert.equal(DEFAULT_PRESET, 'balanced');
assert.equal(PRESET_KEY, 'launcher.screenPreset');
assert.deepEqual(PRESETS.map((p) => p.label), ['Sharp', 'Balanced', 'Data saver']);
for (const p of PRESETS) assert.ok(presetNote(p.id).length > 10, 'each preset says what it does');
assert.equal(presetNote('nope'), '');

// A stored value: a known id, whatever its case; anything else is Balanced.
assert.equal(presetChoice('sharp'), 'sharp');
assert.equal(presetChoice(' Saver '), 'saver');
for (const bad of [null, undefined, '', 'high', 42, {}, 'sharp2']) {
  assert.equal(presetChoice(bad), 'balanced', 'unknown falls back: ' + String(bad));
}

// The readout over a known sequence. Five frames of 10 000 bytes, one every
// 200 ms from t = 0, each acked 50 ms after it arrived; read at t = 1000, one
// second after the readout started: 5 frames and 50 000 bytes in 1 s.
const known = new Readout(0);
for (let i = 0; i < 5; i++) {
  known.frame(10000, i * 200);
  known.acked(i * 200 + 50);
}
let snap = known.snapshot(1000);
assert.equal(snap.frames, 5);
assert.equal(snap.fps, 5);
assert.equal(snap.kBps, 50, '50 000 bytes in 1 s is 50 kB/s');
assert.equal(snap.avgKB, 10);
// The first frame had no ack before it; the next four arrived 150 ms after the ack.
assert.equal(snap.ackMs, 150);

// Frames of different sizes: the average is total / count, not a mean of rates.
const mixed = new Readout(0);
mixed.frame(1000, 0);
mixed.frame(3000, 500);
mixed.frame(8000, 900);
snap = mixed.snapshot(1000);
assert.equal(snap.frames, 3);
assert.equal(snap.kBps, 12, '12 000 bytes in 1 s');
assert.equal(snap.avgKB, 4);
assert.equal(snap.fps, 3);
assert.equal(snap.ackMs, null, 'no ack before any later frame: no gap yet');

// A gap is ack to the next frame, per frame, and averaged.
const gaps = new Readout(0);
gaps.frame(100, 0);
gaps.acked(10);
gaps.frame(100, 110);   // 100 ms
gaps.acked(120);
gaps.frame(100, 440);   // 320 ms
assert.equal(gaps.snapshot(1000).ackMs, 210);
// A second frame with no ack in between does not invent a gap.
gaps.frame(100, 500);
assert.equal(gaps.snapshot(1000).ackMs, 210);

// A window younger than the minimum span is measured over the minimum, so a
// first frame does not read as an enormous rate.
const young = new Readout(0);
young.frame(2000, 100);
snap = young.snapshot(200);
assert.equal(READOUT_MIN_SPAN_MS, 1000);
assert.equal(snap.fps, 1);
assert.equal(snap.kBps, 2);

// Old frames fall out of the window, and the span stops growing at the window.
const aging = new Readout(0);
aging.frame(5000, 0);
aging.frame(5000, 4000);
assert.equal(READOUT_WINDOW_MS, 5000);
snap = aging.snapshot(6000);
assert.equal(snap.frames, 1, 'the frame at t = 0 is older than 5 s');
assert.equal(snap.fps, 1 / 5);
assert.equal(snap.kBps, 1);
snap = aging.snapshot(60000);
assert.equal(snap.frames, 0);
assert.equal(snap.fps, 0);
assert.equal(snap.avgKB, 0);
assert.equal(snap.ackMs, null);

// The lines the overlay shows, from the same numbers.
const lines = readoutLines(known.snapshot(1000), {
  grab: 12.3, digest: 3.1, resize: 9, encode: null, frames: 5,
});
assert.deepEqual(lines, [
  '5.0 fps · 50 kB/s',
  'avg 10.0 kB · ack to frame 150 ms',
  'PC grab 12.3 · digest 3.1 ms',
  'PC resize 9 · encode – ms',
]);
assert.deepEqual(readoutLines(known.snapshot(1000), null).slice(2), ['PC times: waiting…']);
assert.equal(readoutLines(new Readout(0).snapshot(1000), null)[1], 'no frames (a still screen sends none)');

console.log('OK');
