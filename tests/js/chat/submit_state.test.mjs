// Pin for a send's submit fate (static/chat/submit-state.js): only the five
// values the session-host sends are a SubmitState, anything else (an older
// session-host's missing field, an unknown word) is null, so the send verdict
// falls back to its `delivered` reading and never shows it as a landed or
// queued send. Prints the declared values for the Python side to compare.
// Invoked by tests/unit/sessions/test_submit_state.py.

import assert from 'node:assert/strict';

const { SubmitState, submitState } =
  await import('../../../app/webapp/static/chat/submit-state.js');

assert.ok(Object.isFrozen(SubmitState));
const states = Object.values(SubmitState);

for (const value of states) {
  assert.equal(submitState({ submit_state: value }), value);
}
for (const value of [null, undefined, '', 'Confirmed', 'sent', 'queued', 0, true, {}]) {
  assert.equal(submitState({ submit_state: value }), null, String(value));
}
assert.equal(submitState({}), null);

console.log('VALUES ' + JSON.stringify([...states].sort()));
console.log('OK');
