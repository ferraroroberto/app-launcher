/* The wide desktop layout (#1135, design.md Layout "Wide layout").
 *
 * At this query the nav is a left rail and the Code tab is list-and-detail:
 * the session view (#terminalOverlay) docks beside the session list instead
 * of covering it. CSS owns the placement (styles.css "wide layout"); this is
 * the one JS spelling of the same breakpoint, for the few behaviours that
 * change with it. Below it, or on a coarse pointer, nothing changes.
 */

export const WIDE_LAYOUT_QUERY = '(min-width: 1100px) and (pointer: fine)';

export function isWideLayout() {
  try {
    return window.matchMedia(WIDE_LAYOUT_QUERY).matches;
  } catch (_) {
    return false;
  }
}
