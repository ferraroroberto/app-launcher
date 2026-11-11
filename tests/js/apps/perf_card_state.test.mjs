// Pin for the Apps tab Performance card's read state (static/apps/perf-card-state.js):
// the card is in exactly one of its three states for every read, an unreadable
// ledger or a failed fetch is Unknown and never OK, and nothing before the first
// answer reads as OK. Prints the declared values and the unreadable ledger value
// for the Python side to compare.
// Invoked by tests/unit/perf_ledger/test_perf_card_state.py.

import assert from 'node:assert/strict';

const { PerfCardState, LEDGER_UNREADABLE, perfCardState } =
  await import('../../../app/webapp/static/apps/perf-card-state.js');

assert.ok(Object.isFrozen(PerfCardState));
const states = Object.values(PerfCardState);

assert.equal(perfCardState({ ledger: null, failed: false }), PerfCardState.LOADING);
assert.equal(perfCardState({ ledger: null, failed: true }), PerfCardState.UNKNOWN);
assert.equal(perfCardState({ ledger: LEDGER_UNREADABLE, failed: false }), PerfCardState.UNKNOWN);
for (const ledger of ['ok', 'missing']) {
  assert.equal(perfCardState({ ledger, failed: false }), PerfCardState.OK, ledger);
  // A failed fetch on top of a good read keeps the good read only when the
  // caller did not mark it failed; a marked failure is never OK.
  assert.equal(perfCardState({ ledger, failed: true }), PerfCardState.UNKNOWN, ledger);
}
for (const ledger of [null, 'ok', 'missing', LEDGER_UNREADABLE, 'weird']) {
  for (const failed of [false, true]) {
    assert.ok(states.includes(perfCardState({ ledger, failed })), `${ledger}/${failed}`);
  }
}

console.log('VALUES ' + JSON.stringify([...states].sort()));
console.log('UNREADABLE ' + JSON.stringify(LEDGER_UNREADABLE));
console.log('OK');
