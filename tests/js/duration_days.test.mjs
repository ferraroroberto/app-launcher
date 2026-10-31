// Unit pin for the days rollover in the two duration formatters (#1523):
// format.js fmtDuration's granular mode (Coding tab session age) and
// jobs-row.js formatDuration (Jobs tab run duration). Runs under plain Node;
// jobs-row.js pulls in the DOM-bound module graph, so the browser globals it
// touches at import time are stubbed first. Invoked by
// tests/unit/core/test_duration_days.py.

import assert from 'node:assert/strict';
import './_browser_stubs.mjs';

const { fmtDuration } = await import('../../app/webapp/static/shared/format.js');
const { formatDuration } = await import('../../app/webapp/static/jobs/jobs-row.js');

const H = 3600;
const granular = (secs) => fmtDuration(secs, { granular: true });

// fmtDuration granular: 59 s, 59 m, 23 h 59 m unchanged; 24 h and up roll to days.
assert.equal(granular(59), '59s');
assert.equal(granular(59 * 60), '59m');
assert.equal(granular(23 * H + 59 * 60), '23h 59m');
assert.equal(granular(24 * H), '1d 0h');
assert.equal(granular(268 * H + 12 * 60), '11d 4h');

// Non-granular (Board chip) already rolled into days and must not change.
assert.equal(fmtDuration(268 * H + 12 * 60), '11d');
assert.equal(fmtDuration(3 * H), '3h');

// fromEpoch input shape takes the same path.
const nowSecs = Math.floor(Date.now() / 1000);
assert.equal(fmtDuration(nowSecs - (268 * H + 12 * 60), { fromEpoch: true, granular: true }), '11d 4h');

// jobs-row formatDuration: same thresholds, its own sub-day shape.
assert.equal(formatDuration(59), '59s');
assert.equal(formatDuration(59 * 60), '59m');
assert.equal(formatDuration(23 * H + 59 * 60), '23h 59m');
assert.equal(formatDuration(24 * H), '1d 0h');
assert.equal(formatDuration(268 * H + 12 * 60), '11d 4h');
assert.equal(formatDuration(2 * H), '2h');

console.log('OK');
