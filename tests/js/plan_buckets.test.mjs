// Unit pin for the client's reading of the Board's one per-issue partition:
// plan-buckets.js groups the chief's plan rows by the bucket the server
// stamped on each Backlog issue, so the plan card's group counts equal the
// Backlog's. Invoked by tests/unit/static_ui/test_plan_buckets.py.

import assert from 'node:assert/strict';

const { bucketIndex, bucketCounts, groupPlanRows, issueKey, bucketName, CLOSED, NOT_AN_ISSUE, NOT_LOADED } =
  await import('../../app/webapp/static/board/plan-buckets.js');

const card = (repo, number, bucket) => ({ repo, number, bucket });
const backlog = [
  card('app-launcher', 1, 'in_progress'),
  card('app-launcher', 2, 'waiting'),
  card('app-launcher', 3, 'queued'),
  card('app-launcher', 4, 'blocked'),
  card('App-Launcher', 5, 'blocked'),
  card('app-launcher', 6, 'umbrella'),
  card('app-launcher', 7, 'not_planned'),
  card('sleepy', 8, 'dormant'),
  card('app-launcher', 90, 'record'),
];

// Keys: a bare #N takes the row's repo; case folds; no issue is ''.
assert.equal(issueKey('app-launcher', '#3'), 'app-launcher#3');
assert.equal(issueKey('', 'Life-OS#4'), 'life-os#4');
assert.equal(issueKey('app-launcher', ''), '');

// The buckets add up to the total, every card counted once.
const tally = bucketCounts(backlog);
assert.equal(tally.total, backlog.length);
assert.equal(tally.counts.blocked, 2);
assert.equal(tally.counts.record, 1);

const index = bucketIndex(backlog);
const rows = [
  { repo: 'app-launcher', ref: '#3', status: 'queued' },
  { repo: 'app-launcher', ref: '#3', status: 'building' },  // repeat: dropped
  { repo: 'app-launcher', ref: '#4', status: 'parked' },
  { repo: '', ref: 'app-launcher#5', status: 'parked' },
  { repo: 'app-launcher', ref: '#6', status: 'parked' },
  { repo: 'app-launcher', ref: '#2', status: 'parked' },
  { repo: 'app-launcher', ref: '#1', status: 'building' },
  { repo: 'app-launcher', ref: '#99', status: 'merged' },
  { repo: 'automation', ref: '', status: 'waiting-roberto' },
];
const groups = groupPlanRows(rows, index);
const n = (key) => (groups[key] || []).length;

// The plan's Queued, Blocked and Umbrellas equal the Backlog's buckets.
for (const key of ['queued', 'blocked', 'umbrella']) {
  assert.equal(n(key), tally.counts[key], key);
}
// Waiting on you in the plan is part of the Backlog's.
assert.ok(n('waiting') <= tally.counts.waiting);
assert.equal(n('in_progress'), 1);
assert.equal(n(CLOSED), 1);
assert.equal(n(NOT_AN_ISSUE), 1);
assert.equal(bucketName('blocked'), 'Blocked');
assert.equal(bucketName(CLOSED), 'Closed');

// An issue the Backlog does not hold is closed only when the plan says so:
// a queued row for an issue the cache has not seen yet is not loaded, never closed.
const missing = groupPlanRows([
  { repo: 'app-launcher', ref: '#500', status: 'queued' },
  { repo: 'app-launcher', ref: '#501', status: 'building' },
  { repo: 'app-launcher', ref: '#502', status: 'merged' },
  { repo: 'app-launcher', ref: '#503', status: 'closed' },
  { repo: 'app-launcher', ref: '#504', status: 'Merged ' },
], index);
assert.deepEqual((missing[NOT_LOADED] || []).map((r) => r.ref), ['#500', '#501']);
assert.deepEqual((missing[CLOSED] || []).map((r) => r.ref), ['#502', '#503', '#504']);
assert.equal(bucketName(NOT_LOADED), 'Not loaded yet');

console.log('OK');
