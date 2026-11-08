/* What the owner checks put on the owner: the one rule behind the Board's
 * "needs you" head line and the Board tab's badge.
 *
 * Pure, with no imports, so the nav badge (home-head.js) can use it without
 * pulling in the Board view and its import cycle. An item answered Pending, Pause, Close or
 * Problem keeps its row but no longer waits on the owner. The server decides
 * that per item as `counted` (src/board/backlog_buckets.py owner_counts): an
 * open item counts only while its issue sits in the Backlog's Waiting on you,
 * so Your turn is the three groups' counted items, and its first two are Waiting on you. An item without the field
 * (no GitHub fetch yet, a stubbed test body) keeps the plain unmarked rule.
 */

export function ownerSection(body) {
  return (body && body.owner_checks) || null;
}

export function ownerItemWaiting(item) {
  return typeof item.counted === 'boolean' ? item.counted : !item.mark;
}

// Your turn's three owner-check groups; the server stamps one on every item
// (src/board/backlog_buckets.py owner_group) and the badge is their counts added.
export const BEFORE_START_GROUP = 'before_start';
export const VALIDATE_TO_CLOSE = 'validate_to_close';
export const TRY_SHIPPED = 'try_shipped';

// An item's group: the server's stamp, else the same rule from its state and
// kind (a stubbed body without the field).
export function ownerGroup(item) {
  if (item.group) return item.group;
  if (String(item.state || '').toUpperCase() === 'CLOSED') return TRY_SHIPPED;
  return item.kind === 'before-start' ? BEFORE_START_GROUP : VALIDATE_TO_CLOSE;
}

// What waits on you, fleet-wide: null until a list has landed (unknown, never
// zero), else the unmarked items. A payload without the section (a stubbed
// test body) has none, as a payload without `live_sessions` keeps its render.
export function ownerNeedsCount(body) {
  const sec = ownerSection(body);
  if (!sec) return 0;
  if (!sec.available) return null;
  return (sec.items || []).filter(ownerItemWaiting).length;
}
