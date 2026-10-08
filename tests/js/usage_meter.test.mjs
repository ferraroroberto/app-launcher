// Unit pin for the usage meter's reading and colour (#1433). Runs under
// plain Node against the real ES module both the Coding tab's full meter and
// the Board's compact one render through. Invoked by tests/test_usage_meter.py.

import assert from 'node:assert/strict';
import {
  DANGER_PCT, USAGE_SHOWS_DEFAULT, meterReading, paceSummary, usageProviders, windowTone,
} from '../../app/webapp/static/usage-meter.js';

const DAY = 24 * 60 * 60 * 1000;
const now = Date.parse('2026-09-10T12:00:00Z');
// A weekly window half elapsed at `now`: the pace reads 50%.
const weekReset = (now + 3.5 * DAY) / 1000;
const fiveReset = (now + 2 * 60 * 60 * 1000) / 1000;

function line(fivePct, weekPct, extra) {
  return Object.assign({
    harness: 'claude', label: 'Claude Code', state: 'available', stale: false,
    five_hour: fivePct == null ? null : { used_percentage: fivePct, resets_at: fiveReset, duration_minutes: 300 },
    weekly: weekPct == null ? null : { used_percentage: weekPct, resets_at: weekReset, duration_minutes: 10080 },
  }, extra || {});
}

// windowTone on its own: pace drives the week, 90% wins over everything.
assert.equal(DANGER_PCT, 90);
assert.equal(windowTone(null, 50), 'none');
assert.equal(windowTone(30, 50), 'accent');
assert.equal(windowTone(50, 50), 'accent');        // on pace is not ahead of it
assert.equal(windowTone(51, 50), 'attention');
assert.equal(windowTone(89, null), 'accent');      // the 5h window has no pace
assert.equal(windowTone(90, null), 'danger');
assert.equal(windowTone(95, 99), 'danger');        // 90%+ even under pace

// Under pace: accent, no chip, resets and pace kept.
let r = meterReading(line(39, 19), now);
assert.equal(r.week.pace, 50);
assert.equal(r.fiveTone, 'accent');
assert.equal(r.weekTone, 'accent');
assert.equal(r.tone, 'accent');
assert.equal(r.stale, false);
assert.equal(r.chip, '');
assert.equal(r.note, '');
assert.equal(r.five.resetsAt, fiveReset);
assert.equal(r.week.resetsAt, weekReset);

// Ahead of pace: the week is attention, and so is the line.
r = meterReading(line(39, 64), now);
assert.equal(r.weekTone, 'attention');
assert.equal(r.fiveTone, 'accent');
assert.equal(r.tone, 'attention');

// 90% or more in either window: danger.
r = meterReading(line(91, 19), now);
assert.equal(r.fiveTone, 'danger');
assert.equal(r.weekTone, 'accent');
assert.equal(r.tone, 'danger');
r = meterReading(line(10, 90), now);
assert.equal(r.weekTone, 'danger');
assert.equal(r.tone, 'danger');

// Stale: the numbers stay, dimmed, with a "stale" chip; the colour still
// follows the reading.
r = meterReading(line(39, 64, { state: 'stale', stale: true }), now);
assert.equal(r.stale, true);
assert.equal(r.chip, 'stale');
assert.equal(r.tone, 'attention');
assert.equal(r.note, '');

// Unknown / unsupported / error with nothing to fall back to: no numbers, the
// state word is kept as text.
for (const [state, word] of [['unknown', 'unknown'], ['unsupported', 'unsupported'], ['error', 'unavailable']]) {
  r = meterReading(line(null, null, { state: state }), now);
  assert.equal(r.five, null);
  assert.equal(r.week, null);
  assert.equal(r.tone, 'none');
  assert.equal(r.note, word);
  assert.equal(r.chip, '');
}
// An unlisted state reads as unknown.
assert.equal(meterReading(line(null, null, { state: 'weird' }), now).note, 'unknown');
assert.equal(meterReading(null, now).note, 'unknown');

// Unknown with a last good reading behind it: that reading, dimmed and
// chipped with the state word, its resets and pace dropped (they may be past).
const last = [
  { used_percentage: 39, resets_at: fiveReset },
  { used_percentage: 64, resets_at: weekReset },
];
r = meterReading(line(null, null, { state: 'unknown' }), now, last);
assert.equal(r.measured, false);
assert.equal(r.stale, true);
assert.equal(r.chip, 'unknown');
assert.equal(r.note, '');
assert.equal(r.five.pct, 39);
assert.equal(r.five.resetsAt, null);
assert.equal(r.week.pace, null);
assert.equal(r.weekTone, 'accent');               // no pace to be ahead of
// ...and a fresh reading wins over the fallback.
assert.equal(meterReading(line(5, 5), now, last).five.pct, 5);

// A past weekly reset gives no pace, so the week cannot be "ahead".
r = meterReading(line(10, 70, { weekly: { used_percentage: 70, resets_at: (now - DAY) / 1000 } }), now);
assert.equal(r.week.pace, null);
assert.equal(r.weekTone, 'accent');

// The Usage card's summary (#1434): the pace in words and tone, nothing
// when nothing is measured.
assert.deepEqual(paceSummary(meterReading(line(20, 30), now)), { text: 'under pace', tone: 'accent' });
assert.deepEqual(paceSummary(meterReading(line(20, 70), now)), { text: 'ahead of pace', tone: 'attention' });
assert.deepEqual(paceSummary(meterReading(line(95, 30), now)), { text: 'nearly used', tone: 'danger' });
assert.deepEqual(paceSummary(meterReading(line(null, null, { state: 'unknown' }), now)), { text: '', tone: 'none' });
assert.deepEqual(paceSummary(null), { text: '', tone: 'none' });

// "Usage shows" (#1451): which providers are drawn. An unknown value is the default.
assert.equal(USAGE_SHOWS_DEFAULT, 'both');
assert.deepEqual(usageProviders('claude'), { claude: true, codex: false });
assert.deepEqual(usageProviders('codex'), { claude: false, codex: true });
assert.deepEqual(usageProviders('both'), { claude: true, codex: true });
assert.deepEqual(usageProviders('none'), { claude: false, codex: false });
assert.deepEqual(usageProviders('Claude'), { claude: true, codex: true });
assert.deepEqual(usageProviders(undefined), { claude: true, codex: true });

console.log('OK');
