/* A Board job or CI attention card's state, as the Board reads it.
 *
 * The server declares the closed set once, in src/board/attention_state.py;
 * a job card (src/board/__init__.py) and a CI card (src/board/ci_runs.py)
 * both carry these values in `state`. This module is the browser's copy, so
 * the Board asks isFailing() / isDismissible() instead of re-spelling the
 * strings. tests/unit/board/test_attention_state.py fails when the set or
 * its groupings drift from the server's.
 */

export const AttentionState = Object.freeze({
  FAILED: 'failed',
  UNCONFIRMED: 'unconfirmed',
  STUCK: 'stuck',
  UNREADABLE: 'unreadable',
  UNKNOWN: 'unknown',
});

const KNOWN = new Set(Object.values(AttentionState));
// Counts toward the Board's "N failing": a failure, or a run that will not finish.
const FAILING = new Set([AttentionState.FAILED, AttentionState.STUCK]);
// A past event the owner may dismiss; stuck and unreadable describe a live problem.
const DISMISSIBLE = new Set([AttentionState.FAILED, AttentionState.UNCONFIRMED]);

// The AttentionState member a raw state is, or null for anything outside the set.
export function attentionState(state) {
  return KNOWN.has(state) ? state : null;
}

// True for a job or CI card that counts as failing; an unknown value never does.
export function isFailing(card) {
  return (card.kind === 'job' || card.kind === 'ci') && FAILING.has(attentionState(card.state));
}

// True for a job card whose finished run the owner may dismiss; a CI card never is.
export function isDismissible(card) {
  return card.kind === 'job' && !!card.run_id && DISMISSIBLE.has(attentionState(card.state));
}
