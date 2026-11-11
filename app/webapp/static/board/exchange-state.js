/* The Board drawer's last-exchange preview state.
 *
 * The preview is in exactly one of four states: LOADING before the first
 * answer from GET /api/board/sessions/<id>/exchange, READY once an exchange
 * is in hand, EMPTY when the server says the session has no exchange yet,
 * and ERROR for any other unavailable reason or a failed fetch.
 * exchangeState() is the one rule mapping the last answer to a state;
 * drawer.js asks it instead of re-spelling the strings, and board.css styles
 * the preview by the same values. tests/unit/board/test_exchange_state.py
 * pins the set, the CSS selectors and that NO_EXCHANGE is the server's reason.
 */

export const ExchangeState = Object.freeze({
  LOADING: 'loading',
  READY: 'ready',
  EMPTY: 'empty',
  ERROR: 'error',
});

// The unavailable reason the server sends for a session with no exchange yet.
export const NO_EXCHANGE = 'no_exchange';

// The preview's state for the last answer: `body` is the route's JSON (null
// before the first answer), `failed` is a fetch that threw.
export function exchangeState(body, failed = false) {
  if (failed) return ExchangeState.ERROR;
  if (body === null || body === undefined) return ExchangeState.LOADING;
  if (body.available) return ExchangeState.READY;
  return body.reason === NO_EXCHANGE ? ExchangeState.EMPTY : ExchangeState.ERROR;
}
