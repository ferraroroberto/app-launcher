// Pin for Settings > Inspiration's X login state (static/settings/login-state.js):
// only a running window counts as open, and a value outside the set is never
// read as any state, so a typo or a new server value cannot leave the login
// button disabled and polling. Prints the declared values for the Python side
// to compare with LoginState.
// Invoked by tests/unit/core/test_inspiration_login_state.py.

import assert from 'node:assert/strict';

const { LoginState, loginState, isOpen } =
  await import('../../../app/webapp/static/settings/login-state.js');

assert.ok(Object.isFrozen(LoginState));

const states = Object.values(LoginState);
for (const state of states) {
  assert.equal(loginState({ state }), state, state);
  assert.equal(isOpen({ state }), state === LoginState.RUNNING, state);
}

for (const unknown of ['', 'Running', 'open', 'unknown', null, undefined, 42]) {
  assert.equal(loginState({ state: unknown }), null, String(unknown));
  assert.equal(isOpen({ state: unknown }), false, String(unknown));
}
assert.equal(loginState(null), null);
assert.equal(isOpen(undefined), false);

console.log('VALUES ' + JSON.stringify([...states].sort()));
console.log('OPEN ' + JSON.stringify(states.filter((s) => isOpen({ state: s })).sort()));
console.log('OK');
