// Unit pin for the shared "how long ago" phrase and the byte formatter in
// shared/format.js: one ladder for every view that says "N min ago", and one
// byte rendering. Runs under plain Node against the real ES module; invoked by
// tests/unit/core/test_ago_format.py.

import assert from 'node:assert/strict';

const { agoFromMinutes, agoFromSeconds, agoText, formatBytes, plural } =
  await import('../../../app/webapp/static/shared/format.js');

const NOW = Date.parse('2026-11-03T12:00:00Z');
const MIN = 60 * 1000;
const HOUR = 60 * MIN;
const iso = (msAgo) => new Date(NOW - msAgo).toISOString();

// The ladder: just now, minutes, hours up to 48, then days.
assert.equal(agoFromMinutes(-5), 'just now');
assert.equal(agoFromMinutes(0), 'just now');
assert.equal(agoFromMinutes(1), '1 min ago');
assert.equal(agoFromMinutes(59), '59 min ago');
assert.equal(agoFromMinutes(60), '1 h ago');
assert.equal(agoFromMinutes(47 * 60 + 59), '47 h ago');
assert.equal(agoFromMinutes(48 * 60), '2 d ago');
assert.equal(agoFromMinutes(3 * 24 * 60), '3 d ago');

// From an ISO stamp; '' for one that can't be read; a stamp ahead of the
// clock is "just now".
assert.equal(agoText(iso(59 * 1000), NOW), 'just now');
assert.equal(agoText(iso(12 * MIN), NOW), '12 min ago');
assert.equal(agoText(iso(3 * HOUR), NOW), '3 h ago');
assert.equal(agoText(iso(-5 * MIN), NOW), 'just now');
assert.equal(agoText(null, NOW), '');
assert.equal(agoText('not a date', NOW), '');
assert.equal(agoText(new Date(Date.now() - 2 * HOUR - MIN).toISOString()), '2 h ago');

// From seconds; null or NaN is unknown, a negative age reads "just now".
assert.equal(agoFromSeconds(null), '');
assert.equal(agoFromSeconds(NaN), '');
assert.equal(agoFromSeconds(-30), 'just now');
assert.equal(agoFromSeconds(10), 'just now');
assert.equal(agoFromSeconds(59.6), '1 min ago');
assert.equal(agoFromSeconds(3600), '1 h ago');
assert.equal(agoFromSeconds(3 * 86400), '3 d ago');

// Bytes: one decimal under 10 of a unit, whole above, null when unknown.
assert.equal(formatBytes(null), null);
assert.equal(formatBytes(-1), null);
assert.equal(formatBytes(NaN), null);
assert.equal(formatBytes(0), '0 B');
assert.equal(formatBytes(512), '512 B');
assert.equal(formatBytes(1536), '1.5 KB');
assert.equal(formatBytes(12 * 1024), '12 KB');
assert.equal(formatBytes(5.5 * 1024 * 1024), '5.5 MB');

assert.equal(plural(1, 'job', 'jobs'), '1 job');
assert.equal(plural(0, 'job', 'jobs'), '0 jobs');
assert.equal(plural(3, 'job', 'jobs'), '3 jobs');

console.log('OK');
