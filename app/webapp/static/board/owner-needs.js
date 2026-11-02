/* What the owner checks put on the owner: the one rule behind the Board's
 * "needs you" head line and the Board tab's badge.
 *
 * Pure, with no imports, so the nav badge (home-head.js) can use it without
 * pulling in the Board view and its import cycle. An item answered Later or
 * Problem keeps its row but no longer waits on the owner.
 */

export function ownerSection(body) {
  return (body && body.owner_checks) || null;
}

export function ownerItemWaiting(item) {
  return !item.mark;
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
