// Pin for the Board's attention-state module (static/board/attention-state.js):
// only a failed or stuck job/CI card counts as failing, only a failed or
// unconfirmed job card with a run id may be dismissed, and a value outside
// the set does neither. Prints the declared values and groupings for the
// Python side to compare with AttentionState.
// Invoked by tests/unit/board/test_attention_state.py.

import assert from 'node:assert/strict';

const { AttentionState, attentionState, isFailing, isDismissible } =
  await import('../../app/webapp/static/board/attention-state.js');

assert.ok(Object.isFrozen(AttentionState));

const states = Object.values(AttentionState);
const job = (state, runId = 'r1') => ({ kind: 'job', state, run_id: runId });
const ci = (state) => ({ kind: 'ci', state });

// A live problem is never dismissible, and a CI card never is.
for (const live of [AttentionState.STUCK, AttentionState.UNREADABLE, AttentionState.UNKNOWN]) {
  assert.equal(isDismissible(job(live)), false, live);
}
for (const state of states) {
  assert.equal(isDismissible(ci(state)), false, 'ci ' + state);
}
// A dismissible state still needs the run it dismisses.
assert.equal(isDismissible(job(AttentionState.FAILED, null)), false);
// An unread CI repo is not a failure: it is never drawn as green, nor as red.
assert.equal(isFailing(ci(AttentionState.UNKNOWN)), false);
assert.equal(isFailing(ci(AttentionState.FAILED)), true);
// Only job and CI cards carry this state.
assert.equal(isFailing({ kind: 'issue', state: AttentionState.FAILED }), false);

for (const unknown of ['', 'Failed', 'running', 'dead', null, undefined, 42]) {
  assert.equal(attentionState(unknown), null, String(unknown));
  assert.equal(isFailing(job(unknown)), false, String(unknown));
  assert.equal(isDismissible(job(unknown)), false, String(unknown));
}

console.log('VALUES ' + JSON.stringify([...states].sort()));
console.log('FAILING ' + JSON.stringify(states.filter((s) => isFailing(job(s))).sort()));
console.log('DISMISSIBLE ' + JSON.stringify(states.filter((s) => isDismissible(job(s))).sort()));
console.log('OK');
