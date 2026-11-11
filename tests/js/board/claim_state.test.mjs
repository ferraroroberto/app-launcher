// Pin for a Backlog card's claim owner liveness (static/board/claim-state.js):
// only the three verdicts the server sends are a ClaimState, anything else
// (no claim, a missing field, an unknown word) is null and never reads as
// dead or unknown. Prints the declared values for the Python side to compare.
// Invoked by tests/unit/board/test_claim_state.py.

import assert from 'node:assert/strict';

const { ClaimState, claimState } =
  await import('../../../app/webapp/static/board/claim-state.js');

assert.ok(Object.isFrozen(ClaimState));
const states = Object.values(ClaimState);

for (const value of states) {
  assert.equal(claimState({ claim_state: value }), value);
}
for (const value of [null, undefined, '', 'zombie', 'DEAD', 'Unknown', 0, true, {}]) {
  assert.equal(claimState({ claim_state: value }), null, String(value));
}
assert.equal(claimState({}), null);

console.log('VALUES ' + JSON.stringify([...states].sort()));
console.log('OK');
