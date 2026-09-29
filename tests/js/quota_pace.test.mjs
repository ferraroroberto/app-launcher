// Unit pin for the quota rows' weekly linear pace (#1330). Runs under plain
// Node against the real ES module both the Coding and Board quota rows
// render through. Invoked by tests/test_quota_pace.py.

import assert from 'node:assert/strict';
import { quotaPace } from '../../app/webapp/static/dom-utils.js';

const DAY = 24 * 60 * 60 * 1000;
const WEEK = 7 * DAY;
const reset = Date.parse('2026-09-11T14:00:00Z');
const epoch = reset / 1000;
const iso = '2026-09-11T14:00:00.000000Z';

// Mid-window reads ~50%, from epoch seconds and from ISO alike.
assert.equal(quotaPace({ resets_at: epoch }, reset - WEEK / 2), 50);
assert.equal(quotaPace({ resets_at: iso }, reset - WEEK / 2), 50);

// Right after a reset: 0%. Just before the next one: 100%, never above.
assert.equal(quotaPace({ resets_at: epoch }, reset - WEEK), 0);
assert.equal(quotaPace({ resets_at: epoch }, reset - WEEK + 60 * 1000), 0);
assert.equal(quotaPace({ resets_at: epoch }, reset), 100);
assert.equal(quotaPace({ resets_at: epoch }, reset - 60 * 1000), 100);

// Clamped at the low end: a reset more than one window away reads 0%.
assert.equal(quotaPace({ resets_at: epoch }, reset - 2 * WEEK), 0);

// Within a point of elapsed / length: Tuesday 14:00 after a Sunday reset.
assert.equal(quotaPace({ resets_at: epoch }, reset - WEEK + 2 * DAY), 29);
assert.equal(quotaPace({ resets_at: epoch }, reset - WEEK + 3.71 * DAY), 53);

// The backend's window length wins over the 7-day default.
assert.equal(quotaPace({ resets_at: epoch, duration_minutes: 300 }, reset - 150 * 60 * 1000), 50);
assert.equal(quotaPace({ resets_at: epoch, duration_minutes: 10080 }, reset - WEEK / 4), 75);

// No pace from a missing, unparseable or already-past reset: a past reset
// means the reading's window is over, so any pace would be stale.
assert.equal(quotaPace(null, reset), null);
assert.equal(quotaPace({ used_percentage: 19 }, reset), null);
assert.equal(quotaPace({ resets_at: null }, reset), null);
assert.equal(quotaPace({ resets_at: 'not a date' }, reset), null);
assert.equal(quotaPace({ resets_at: epoch }, reset + 1000), null);

console.log('quota pace: OK');
