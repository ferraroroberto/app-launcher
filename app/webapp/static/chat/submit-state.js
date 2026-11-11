/* A send's submit fate, as the send verdict reads it.
 *
 * The session-host judges what happened to the Enter of one /input call, in
 * src/session_host_input.py (InputOutcome.submit_state, the SUBMIT_* values),
 * and sends it as the verdict's `submit_state`: CONFIRMED (Enter went in after
 * a verified settle), UNCONFIRMED (Enter went in, nothing verified it landed),
 * PENDING (the deferred watcher still holds the Enter), NOT_SUBMITTED (asked
 * for, never sent) or NOT_REQUESTED (the call asked for no submit).
 * submitState() is the one rule reading the field; sessions.js asks it
 * instead of re-spelling the strings. tests/unit/sessions/test_submit_state.py
 * fails when the set drifts from the server's.
 */

export const SubmitState = Object.freeze({
  CONFIRMED: 'confirmed',
  UNCONFIRMED: 'unconfirmed',
  PENDING: 'pending',
  NOT_SUBMITTED: 'not_submitted',
  NOT_REQUESTED: 'not_requested',
});

const KNOWN = new Set(Object.values(SubmitState));

// The verdict's SubmitState member, or null for an older session-host that
// sends none, or any value outside the set.
export function submitState(verdict) {
  return KNOWN.has(verdict.submit_state) ? verdict.submit_state : null;
}
