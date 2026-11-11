/* A pending input entry's state, as the Chat pane reads it.
 *
 * The session-host holds each message it accepted but the agent has not taken
 * yet, in src/session_hosting/pending_input.py, and sends its place as the
 * entry's `state`: IN_COMPOSER (in the agent's input box, Enter still with
 * the watcher), QUEUED (Enter went in mid-turn, the agent's own queue holds
 * it) or DISCARDING (the owner discarded it, the watcher clears it instead of
 * pressing Enter). pendingState() is the one rule reading the field;
 * chat-pending.js asks it instead of re-spelling the strings.
 * tests/unit/sessions/test_pending_state.py fails when the set drifts from
 * the server's.
 */

export const PendingState = Object.freeze({
  IN_COMPOSER: 'in_composer',
  QUEUED: 'queued',
  DISCARDING: 'discarding',
});

const KNOWN = new Set(Object.values(PendingState));

// The entry's PendingState member, or null for any value outside the set.
export function pendingState(entry) {
  return KNOWN.has(entry.state) ? entry.state : null;
}

// A queued entry the agent's queued markers have not confirmed yet.
export function isUnconfirmed(entry) {
  return pendingState(entry) === PendingState.QUEUED && entry.confirmed === false;
}
