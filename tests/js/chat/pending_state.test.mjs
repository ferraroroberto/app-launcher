// Pin for a pending input entry's state (static/chat/pending-state.js): only
// the three places the session-host sends are a PendingState, anything else
// (a missing field, an unknown word) is null and never reads as queued, so it
// is never shown as waiting for the agent's confirmation. Prints the declared
// values for the Python side to compare.
// Invoked by tests/unit/sessions/test_pending_state.py.

import assert from 'node:assert/strict';

const { PendingState, pendingState, isUnconfirmed } =
  await import('../../../app/webapp/static/chat/pending-state.js');

assert.ok(Object.isFrozen(PendingState));
const states = Object.values(PendingState);

for (const value of states) {
  assert.equal(pendingState({ state: value }), value);
}
for (const value of [null, undefined, '', 'Queued', 'sent', 'pending', 0, true, {}]) {
  assert.equal(pendingState({ state: value }), null, String(value));
  assert.equal(isUnconfirmed({ state: value, confirmed: false }), false, String(value));
}
assert.equal(pendingState({}), null);

assert.equal(isUnconfirmed({ state: PendingState.QUEUED, confirmed: false }), true);
assert.equal(isUnconfirmed({ state: PendingState.QUEUED, confirmed: true }), false);
assert.equal(isUnconfirmed({ state: PendingState.QUEUED }), false);
assert.equal(isUnconfirmed({ state: PendingState.IN_COMPOSER, confirmed: false }), false);

console.log('VALUES ' + JSON.stringify([...states].sort()));
console.log('OK');
