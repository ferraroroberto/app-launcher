/* The "↓ Latest" pill (#981 → #1140): one control, three mounts.
 *
 * The Terminal, the session overlay's Chat pane and the Life OS conversation
 * viewer all float the same bottom-right pill while the newest content is
 * off screen, and one tap takes the reader back to it. This module owns the
 * shared part: the show/hide rule's plumbing (checked once per frame, hidden
 * as the safe default when a read throws) and the tap. A surface supplies a
 * source — `isAway()` (is the latest content out of view?) and `jump()` (go
 * there) — and tells the pill when to re-check.
 *
 * Two sources exist: the Terminal adapts xterm's buffer (terminal-bar.js,
 * keeping its own "more than one screen of scrollback" threshold), and
 * `mountScrollerPill` below adapts any scrollable element, which is what the
 * Chat pane and the viewer are.
 */

// How far above the end of a scrollable element still counts as "at the
// latest". The Chat pane's stick-to-bottom (#1050) uses the same number, so
// the pill shows exactly when new turns would stop scrolling into view.
export const LATEST_SLACK_PX = 120;

export function scrollerIsAway(box) {
  return box.scrollHeight - box.scrollTop - box.clientHeight > LATEST_SLACK_PX;
}

// Wire `button` to `source`. Returns { update, schedule }: update() re-checks
// now, schedule() coalesces any number of calls into one check next frame.
export function createLatestPill(button, source) {
  let frame = 0;
  function update() {
    let show = false;
    try { show = !!source.isAway(); } catch (_) { /* hidden is the safe default */ }
    if (button.hidden === show) button.hidden = !show;
  }
  function schedule() {
    if (frame) return;
    frame = window.requestAnimationFrame(function () {
      frame = 0;
      update();
    });
  }
  button.addEventListener('click', function () {
    try { source.jump(); } catch (_) {}
    update();
  });
  return { update: update, schedule: schedule };
}

// The pill over a scrollable element (`box`). It re-checks on scroll and
// whenever the box or anything directly in it changes size — content landing
// (a live turn, a finished render) moves the end without a scroll event.
// A hidden box has no geometry, so it never shows the pill.
export function mountScrollerPill(button, box) {
  const pill = createLatestPill(button, {
    isAway: function () { return box.clientHeight > 0 && scrollerIsAway(box); },
    jump: function () { box.scrollTop = box.scrollHeight; },
  });
  box.addEventListener('scroll', pill.schedule, { passive: true });
  if (window.ResizeObserver) {
    const ro = new ResizeObserver(pill.schedule);
    ro.observe(box);
    Array.prototype.forEach.call(box.children, function (c) { ro.observe(c); });
  }
  return pill;
}
