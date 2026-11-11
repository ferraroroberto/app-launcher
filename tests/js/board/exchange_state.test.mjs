// Pin for the Board drawer's last-exchange preview state
// (static/board/exchange-state.js): every answer maps to exactly one of the
// four states, an unavailable answer or a failed fetch is never Ready, only
// the server's no-exchange reason reads as Empty, and nothing before the first
// answer reads as Ready. Prints the declared values and NO_EXCHANGE for the
// Python side to compare.
// Invoked by tests/unit/board/test_exchange_state.py.

import assert from 'node:assert/strict';

const { ExchangeState, NO_EXCHANGE, exchangeState } =
  await import('../../../app/webapp/static/board/exchange-state.js');

assert.ok(Object.isFrozen(ExchangeState));
const states = Object.values(ExchangeState);

assert.equal(exchangeState(null), ExchangeState.LOADING);
assert.equal(exchangeState(undefined), ExchangeState.LOADING);
assert.equal(exchangeState(null, true), ExchangeState.ERROR);
assert.equal(exchangeState({ available: true }), ExchangeState.READY);
assert.equal(exchangeState({ available: false, reason: NO_EXCHANGE }), ExchangeState.EMPTY);
for (const reason of ['session_not_found', 'session_host_unreachable', 'native_unavailable',
  'capture_unparseable', 'weird', null]) {
  assert.equal(exchangeState({ available: false, reason }), ExchangeState.ERROR, String(reason));
}
// A failed fetch is Error whatever answer came before it.
for (const body of [null, { available: true }, { available: false, reason: NO_EXCHANGE }]) {
  assert.equal(exchangeState(body, true), ExchangeState.ERROR, JSON.stringify(body));
}
for (const body of [null, {}, { available: true }, { available: false }, { available: false, reason: NO_EXCHANGE }]) {
  for (const failed of [false, true]) {
    const got = exchangeState(body, failed);
    assert.ok(states.includes(got), `${JSON.stringify(body)}/${failed}`);
    if (failed || !body || !body.available) assert.notEqual(got, ExchangeState.READY);
  }
}

console.log('VALUES ' + JSON.stringify([...states].sort()));
console.log('NO_EXCHANGE ' + JSON.stringify(NO_EXCHANGE));
console.log('OK');
