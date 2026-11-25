// Unit pin for how the Sync all dialog and toast read a run: the progress
// line, the counts, the closing summary and the order of the report's lines.
// Runs under plain Node against the real ES module. Invoked by
// tests/unit/scanner/test_git_sync_model_js.py.

import assert from 'node:assert/strict';
import {
  isRunning, sortResults, syncCounts, syncProgress, syncSummary,
} from '../../../app/webapp/static/apps/git-sync-model.js';

const res = (name, outcome, reason = '', commits = 0) => ({ id: name, name, outcome, reason, commits });
const run = (over) => ({
  started_at: '2026-11-25T10:00:00Z', finished_at: null, total: 26, done: 2, current: 'atlas-web',
  error: null, results: [], ...over,
});

// While it runs: the count is the project being worked on, never past the total.
assert.equal(syncProgress(null), 'Fetching and fast-forwarding…');
assert.equal(syncProgress(run({ total: 0 })), 'Fetching and fast-forwarding…');
assert.equal(syncProgress(run()), 'Syncing 3 of 26 · atlas-web');
assert.equal(syncProgress(run({ current: null })), 'Syncing 3 of 26');
assert.equal(syncProgress(run({ done: 26 })), 'Syncing 26 of 26 · atlas-web');
assert.equal(isRunning(run()), true);
assert.equal(isRunning(run({ finished_at: '2026-11-25T10:01:00Z' })), false);
assert.equal(isRunning(null), false);

// A summary exists only for a finished run.
assert.equal(syncSummary(run()), null);
assert.equal(syncSummary(null), null);

const finished = run({
  finished_at: '2026-11-25T10:01:00Z', done: 8, total: 8,
  results: [
    res('a', 'pulled', '', 2), res('b', 'pulled', '', 1), res('c', 'up-to-date'), res('d', 'up-to-date'),
    res('e', 'up-to-date'), res('f', 'skipped', 'dirty'), res('g', 'skipped', 'dirty'),
    res('h', 'skipped', 'not on main'),
  ],
});
assert.deepEqual(syncCounts(finished), { pulled: 2, current: 3, skipped: 3, failed: 0 });
// A skip is counted and its reasons named once each; it is not a failure.
assert.deepEqual(syncSummary(finished), {
  text: '2 fast-forwarded · 3 already current · 3 skipped (dirty, not on main)', kind: '',
});

// A failure turns it red and names the projects, a long list shortened.
const failed = run({
  finished_at: 'x', total: 7, done: 7,
  results: [res('a', 'pulled', '', 1), res('w', 'failed', 'boom'), res('x', 'failed', 'boom'),
    res('y', 'failed', 'boom'), res('z', 'failed', 'boom')],
});
assert.deepEqual(syncSummary(failed), {
  text: '1 fast-forwarded · 0 already current · 4 failed: w, x, y and 1 more', kind: 'error',
});

// Nothing skipped: no skipped clause.
assert.equal(syncSummary(run({ finished_at: 'x', results: [res('a', 'up-to-date')] })).text, '0 fast-forwarded · 1 already current');

// A run that broke says so, red.
assert.deepEqual(syncSummary(run({ finished_at: 'x', error: 'OSError: disk' })), {
  text: 'Sync all stopped: OSError: disk', kind: 'error',
});

// The report lists failures, then skips, then fast-forwards, then current ones; by name inside a group.
assert.deepEqual(
  sortResults([res('b', 'up-to-date'), res('c', 'pulled'), res('a', 'skipped', 'dirty'), res('d', 'failed', 'x'), res('a2', 'up-to-date')]).map((r) => r.name),
  ['d', 'a', 'c', 'a2', 'b'],
);
assert.deepEqual(sortResults(undefined), []);

console.log('OK');
