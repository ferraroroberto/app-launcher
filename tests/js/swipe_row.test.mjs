// Unit pin for the swipe gesture's decisions (#1503): swipe-row.js keeps
// them as plain functions of numbers, because Playwright's touch emulation
// only taps, so thresholds, the angle lock, the edge zone, commit and cancel
// are pinned here under plain Node. Invoked by tests/test_swipe_row.py.

import assert from 'node:assert/strict';

const { SWIPE, lockAxis, startsInEdge, clampOffset, isArmed, release } =
  await import('../../app/webapp/static/swipe-row.js');

const W = 390;  // a phone row

// The numbers the issue specifies.
assert.equal(SWIPE.deadZone, 8);
assert.equal(SWIPE.edgeZone, 24);
assert.equal(SWIPE.paneWidth, 88);
assert.equal(SWIPE.fullRatio, 0.55);
assert.equal(SWIPE.flickRatio, 0.30);

// Dead zone: nothing is decided inside 8px.
assert.equal(lockAxis(7, 0), null);
assert.equal(lockAxis(0, 7), null);
assert.equal(lockAxis(-7, 5), null);
// Horizontal only when it clearly dominates; vertical wins otherwise.
assert.equal(lockAxis(12, 2), 'x');
assert.equal(lockAxis(-12, 2), 'x');
assert.equal(lockAxis(11, 8), null);    // 11 < 8 * 1.5: undecided, keep reading
assert.equal(lockAxis(12, 8), 'x');     // exactly 1.5x counts as horizontal
assert.equal(lockAxis(9, 9), 'y');      // a diagonal is a scroll, never a swipe
assert.equal(lockAxis(2, 30), 'y');
assert.equal(lockAxis(3, -30), 'y');

// The leftmost 24px belong to the system back gesture.
assert.equal(startsInEdge(0), true);
assert.equal(startsInEdge(23), true);
assert.equal(startsInEdge(24), false);
assert.equal(startsInEdge(200), false);

// No movement toward a side without an action; never past the row.
const both = { leading: {}, trailing: {} };
assert.equal(clampOffset(50, W, { leading: null, trailing: {} }), 0);
assert.equal(clampOffset(-50, W, { leading: {}, trailing: null }), 0);
assert.equal(clampOffset(500, W, both), W);
assert.equal(clampOffset(-500, W, both), -W);
assert.equal(clampOffset(-120, W, both), -120);

// Armed past ~55% of the row, either way.
assert.equal(isArmed(0.55 * W, W), true);
assert.equal(isArmed(0.54 * W, W), false);
assert.equal(isArmed(-0.6 * W, W), true);

// Release: a full swipe commits, slow or not.
assert.equal(release(0.6 * W, 0, W, false), 'commit');
assert.equal(release(-0.6 * W, 0, W, false), 'commit');
// A fast flick past 30% commits; the same distance slowly only rests open.
assert.equal(release(0.35 * W, 0.8, W, false), 'commit');
assert.equal(release(-0.35 * W, -0.8, W, false), 'commit');
assert.equal(release(0.35 * W, 0.1, W, false), 'open');
// A flick back toward the start is not a commit.
assert.equal(release(0.35 * W, -0.8, W, false), 'open');
// A fast flick short of 30% is not one either.
assert.equal(release(0.2 * W, 1.5, W, false), 'open');
// Past half a pane it rests open; short of that it cancels.
assert.equal(release(44, 0, W, false), 'open');
assert.equal(release(43, 0, W, false), 'close');
assert.equal(release(-20, 0, W, false), 'close');
assert.equal(release(0, 3, W, false), 'close');
// A disabled action never commits; it may rest open to show why.
assert.equal(release(0.9 * W, 2, W, true), 'open');
assert.equal(release(20, 0, W, true), 'close');

console.log('OK');
