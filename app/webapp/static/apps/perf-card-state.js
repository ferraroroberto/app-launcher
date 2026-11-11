/* The Apps tab Performance card's read state.
 *
 * The card is in exactly one of three states: LOADING before the first answer
 * from GET /api/apps/perf, UNKNOWN when the ledger cannot be read (the server
 * says `unreadable`) or the fetch failed with nothing good to show, and OK
 * once a readable answer is in hand. perfCardState() is the one rule mapping
 * the last read to a state; apps-perf.js asks it instead of re-spelling the
 * strings. tests/unit/perf_ledger/test_perf_card_state.py pins the set and
 * that the unreadable value matches src/perf_ledger's LEDGER_UNREADABLE.
 */

export const PerfCardState = Object.freeze({
  LOADING: 'loading',
  UNKNOWN: 'unknown',
  OK: 'ok',
});

// The ledger value the server sends for a file it could not read.
export const LEDGER_UNREADABLE = 'unreadable';

// The card's state for the last read: `ledger` is the server's ledger value
// (null before the first answer), `failed` is a fetch that failed with no good read.
export function perfCardState(read) {
  if (read.failed || read.ledger === LEDGER_UNREADABLE) return PerfCardState.UNKNOWN;
  if (read.ledger === null) return PerfCardState.LOADING;
  return PerfCardState.OK;
}
