/* Native iOS momentum (fling) scrolling for the phone terminal — #23.
 *
 * xterm's scroll container, `.xterm-viewport`, is a genuine
 * `overflow-y:scroll` element with a full-height scroll area — iOS
 * grants it native inertial scrolling for free. The problem is
 * *reaching* it: `.xterm-screen` (the text layer) sits on top of the
 * viewport and is ~20px wider-reaching, so a finger almost always
 * lands on the text, not the viewport. xterm then scrolls the viewport
 * programmatically, with no momentum.
 *
 * `enableNativeTouchScroll()` hands the whole surface to native
 * scrolling:
 *   1. `.xterm-screen` gets `pointer-events:none`, so every touch
 *      falls through to `.xterm-viewport` and iOS owns the gesture.
 *   2. xterm's own touch handler (bound to the `.xterm` root) would
 *      still scroll programmatically *and* call preventDefault — which
 *      cancels the native momentum. A capture-phase listener swallows
 *      the touch events (`stopPropagation`, never `preventDefault`) so
 *      xterm's handler never runs and iOS keeps the fling.
 *   3. A stationary tap is detected and re-focuses the terminal, so
 *      the on-screen keyboard still opens (xterm's own tap-to-focus is
 *      gone with the text layer non-interactive).
 *
 * Trade-off: with the text layer non-interactive, xterm's touch-based
 * text selection and link taps are gone on the phone — an accepted
 * trade for consistent native scrolling.
 *
 * Phone only — the caller skips this for the PC mirror window, which
 * scrolls with a wheel and should keep mouse text-selection.
 */

// Max finger travel (px) and duration (ms) for a touch to count as a
// tap rather than a scroll — a tap re-focuses the terminal.
const TAP_MAX_TRAVEL_PX = 10;
const TAP_MAX_DURATION_MS = 500;

/**
 * Route all terminal touches to native `.xterm-viewport` scrolling.
 *
 * @param {object} term  the xterm Terminal instance (needs `.element`
 *        and `.focus()`).
 * @returns {() => void}  disposer — removes listeners and restores
 *        `.xterm-screen` interactivity.
 */
export function enableNativeTouchScroll(term) {
  const el = term && term.element;
  const screen = el && el.querySelector('.xterm-screen');
  if (!el || !screen) return function () {};

  const prevPointerEvents = screen.style.pointerEvents;
  screen.style.pointerEvents = 'none';

  let startX = 0;
  let startY = 0;
  let startT = 0;
  const swallow = function (e) { e.stopPropagation(); };
  const onStart = function (e) {
    e.stopPropagation();
    const touch = e.touches && e.touches[0];
    if (touch) {
      startX = touch.clientX;
      startY = touch.clientY;
      startT = e.timeStamp;
    }
  };
  const onEnd = function (e) {
    e.stopPropagation();
    // A short, near-stationary touch is a tap: re-focus the terminal
    // (inside the touch gesture, so iOS still raises the keyboard).
    const touch = e.changedTouches && e.changedTouches[0];
    if (!touch) return;
    const moved = Math.hypot(touch.clientX - startX, touch.clientY - startY);
    if (moved < TAP_MAX_TRAVEL_PX && (e.timeStamp - startT) < TAP_MAX_DURATION_MS) {
      try { term.focus(); } catch (_) { /* best effort */ }
    }
  };

  // Capture phase: these fire before xterm's bubble-phase touch
  // listeners on the same root element, and stopPropagation keeps the
  // event from ever reaching them. passive:true — we never call
  // preventDefault, so the browser's native scroll/fling is untouched.
  const opts = { capture: true, passive: true };
  el.addEventListener('touchstart', onStart, opts);
  el.addEventListener('touchmove', swallow, opts);
  el.addEventListener('touchend', onEnd, opts);
  el.addEventListener('touchcancel', swallow, opts);

  return function dispose() {
    el.removeEventListener('touchstart', onStart, opts);
    el.removeEventListener('touchmove', swallow, opts);
    el.removeEventListener('touchend', onEnd, opts);
    el.removeEventListener('touchcancel', swallow, opts);
    screen.style.pointerEvents = prevPointerEvents;
  };
}
