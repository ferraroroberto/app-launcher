/* A Backlog card's claim owner liveness, as the Board reads it.
 *
 * The server judges each active-issue claim's owner once, in
 * src/board/active_issue_claims.py, and sends the verdict as the card's
 * `claim_state`: LIVE (the lane is running), DEAD (the lane is provably
 * gone, so the issue is startable again) or UNKNOWN (the owner could not be
 * verified, so the in-progress lock holds). A card with no claim has none.
 * claimState() is the one rule reading the field; board.js asks it instead of
 * re-spelling the strings. tests/unit/board/test_claim_state.py fails when the
 * set drifts from the server's.
 */

export const ClaimState = Object.freeze({
  LIVE: 'live',
  DEAD: 'dead',
  UNKNOWN: 'unknown',
});

const KNOWN = new Set(Object.values(ClaimState));

// The card's ClaimState member, or null for no claim or any value outside the set.
export function claimState(card) {
  return KNOWN.has(card.claim_state) ? card.claim_state : null;
}
