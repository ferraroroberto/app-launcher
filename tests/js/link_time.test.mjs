// Unit pin for the Links sheet's time label: relative inside the first day,
// the day and time after. Runs under plain Node; invoked by
// tests/unit/static_ui/test_link_time.py.

import assert from 'node:assert/strict';

const { cutoffLabel, timeLabel } = await import('../../app/webapp/static/links/link-time.js');

const NOW = Date.parse('2026-11-03T12:00:00Z');
const ago = (ms) => new Date(NOW - ms).toISOString();
const MIN = 60 * 1000;
const HOUR = 60 * MIN;

assert.equal(timeLabel(ago(0), NOW), 'just now');
assert.equal(timeLabel(ago(59 * 1000), NOW), 'just now');
assert.equal(timeLabel(ago(MIN), NOW), '1 min ago');
assert.equal(timeLabel(ago(12 * MIN), NOW), '12 min ago');
assert.equal(timeLabel(ago(59 * MIN), NOW), '59 min ago');
assert.equal(timeLabel(ago(HOUR), NOW), '1 h ago');
assert.equal(timeLabel(ago(23 * HOUR + 59 * MIN), NOW), '23 h ago');

// A stamp slightly ahead of this clock is "just now", never negative minutes.
assert.equal(timeLabel(ago(-5 * MIN), NOW), 'just now');

// Older than a day: the day and time, not a relative age.
const older = timeLabel(ago(24 * HOUR), NOW);
assert.ok(older && !/ago|just now/.test(older), older);
assert.ok(/\d/.test(older), older);

// Unreadable or absent stamps give no label.
assert.equal(timeLabel(null, NOW), '');
assert.equal(timeLabel('', NOW), '');
assert.equal(timeLabel('not a date', NOW), '');

// Where a read stopped: a day and time, never a relative age.
const cut = cutoffLabel('2026-10-31T12:28:00Z');
assert.ok(/Oct|31/.test(cut) && /\d/.test(cut) && !/ago|just now/.test(cut), cut);
assert.equal(cutoffLabel(null), '');
assert.equal(cutoffLabel('not a date'), '');

console.log('OK');
