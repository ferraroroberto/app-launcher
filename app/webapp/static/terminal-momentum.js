/* Touch-momentum (fling) scrolling for the xterm.js terminal — issue #23.
 *
 * xterm already scrolls on a touch drag: it binds touchstart/touchmove
 * to its root element (`.xterm`) and tracks the finger 1:1 into
 * `.xterm-viewport.scrollTop`. What it never does is *fling* — release
 * the finger and the scroll stops dead, with no inertia.
 *
 * This module layers only that missing piece on top. It is purely
 * additive: it listens on the same root element xterm uses (a passive
 * observer — it never calls preventDefault or stopPropagation), tracks
 * the finger's velocity, and on touchend animates a decaying inertial
 * scroll of `.xterm-viewport`. xterm keeps full ownership of the drag
 * itself, so text selection, long-press and double-tap are untouched.
 *
 * Why the root element and not `.xterm-viewport`: the viewport is a
 * *sibling* of `.xterm-screen` (the layer the finger actually touches),
 * so touch events bubble screen → `.xterm` → document and never reach
 * the viewport. Listeners there would simply never fire.
 *
 * Desktop is untouched — only `touch*` listeners are wired, so wheel
 * scrolling never reaches this code.
 */

// --- Tuning constants (expect 1-2 phone passes to settle) ------------
// Pixels the finger must travel before a release is eligible to fling
// at all — keeps a tap (with its few px of jitter) from flinging.
const SLOP_PX = 10;
// Per-frame velocity multiplier during the inertial decay, normalised
// to a 60fps frame. Closer to 1 = longer glide; ~0.95 ≈ a natural
// phone fling.
const FRICTION = 0.95;
// Minimum release speed (px/ms) that triggers a fling at all. Below
// this the scroll just stops where the finger left it.
const MIN_FLING_VELOCITY = 0.05;
// Below this speed the decay loop rounds down to a stop.
const MIN_DECAY_VELOCITY = 0.015;
// Velocity is averaged over touchmove samples no older than this (ms),
// so a pause-then-release ends with ~zero velocity (snap, not fling)
// instead of carrying a stale speed from the start of the drag.
const VELOCITY_WINDOW_MS = 100;
// A reference frame at 60fps — keeps FRICTION frame-rate independent
// when the display runs faster or slower.
const FRAME_MS = 1000 / 60;

/**
 * Wire custom touch-momentum (fling) scrolling onto an xterm terminal.
 *
 * @param {HTMLElement} touchTarget  the xterm root element
 *        (`term.element`) — the element xterm itself binds touch to.
 * @param {HTMLElement} viewport     the `.xterm-viewport` scroll
 *        element whose `scrollTop` the fling animates.
 * @param {{onFlingState?: (active: boolean) => void}} [hooks]
 *        onFlingState fires true when an inertial scroll starts and
 *        false when it settles — the caller uses it to suspend
 *        tail-follow auto-scroll so new output can't fight the fling.
 * @returns {{dispose: () => void}}  removes every listener and cancels
 *        any in-flight fling animation.
 */
export function wireTouchMomentum(touchTarget, viewport, hooks) {
  let tracking = false;     // a single-finger touch is in progress
  let moved = false;        // that touch has travelled past the slop
  let startY = 0;           // clientY at touchstart (slop is measured here)
  let velocity = 0;         // px/ms, positive = finger moving down
  let flingRaf = 0;         // requestAnimationFrame id, 0 when idle
  const samples = [];       // recent {y, t} touch points for velocity

  function setFling(active) {
    if (hooks && typeof hooks.onFlingState === 'function') {
      try { hooks.onFlingState(active); } catch (_) { /* hook is best-effort */ }
    }
  }

  function cancelFling() {
    if (flingRaf) {
      cancelAnimationFrame(flingRaf);
      flingRaf = 0;
      setFling(false);
    }
  }

  function pushSample(y, t) {
    samples.push({ y: y, t: t });
    // Drop samples older than the velocity window, but always keep the
    // last two so a slow drag still has a pair to measure.
    const cutoff = t - VELOCITY_WINDOW_MS;
    while (samples.length > 2 && samples[0].t < cutoff) samples.shift();
  }

  function currentVelocity(now) {
    if (samples.length < 2) return 0;
    const a = samples[0];
    const b = samples[samples.length - 1];
    // Finger held still before lifting (last move older than the
    // window) → a deliberate reposition, not a flick: no fling.
    if (now - b.t > VELOCITY_WINDOW_MS) return 0;
    const dt = b.t - a.t;
    if (dt <= 0) return 0;
    return (b.y - a.y) / dt;
  }

  function maxScrollTop() {
    return Math.max(0, viewport.scrollHeight - viewport.clientHeight);
  }

  function startFling() {
    setFling(true);
    let lastT = performance.now();
    function step(now) {
      flingRaf = 0;
      // Clamp dt so a frame stall (tab refocus, GC pause) can't fling
      // the viewport across the whole buffer in a single jump.
      const dt = Math.min(now - lastT, 32);
      lastT = now;
      // Finger moving down (positive velocity) reveals earlier history,
      // i.e. scrollTop decreases.
      viewport.scrollTop -= velocity * dt;
      velocity *= Math.pow(FRICTION, dt / FRAME_MS);

      const max = maxScrollTop();
      if (viewport.scrollTop <= 0) { viewport.scrollTop = 0; setFling(false); return; }
      if (viewport.scrollTop >= max) { viewport.scrollTop = max; setFling(false); return; }
      if (Math.abs(velocity) < MIN_DECAY_VELOCITY) { setFling(false); return; }
      flingRaf = requestAnimationFrame(step);
    }
    flingRaf = requestAnimationFrame(step);
  }

  function onTouchStart(e) {
    // A second finger (pinch, etc.) is not our gesture — bail out and
    // let xterm / the browser have it.
    if (e.touches.length !== 1) { cancelFling(); tracking = false; return; }
    cancelFling();
    tracking = true;
    moved = false;
    startY = e.touches[0].clientY;
    samples.length = 0;
    pushSample(startY, e.timeStamp || performance.now());
  }

  function onTouchMove(e) {
    // Passive observer: xterm's own touchmove handler does the actual
    // per-drag scroll. We only sample the finger to measure velocity.
    if (!tracking || e.touches.length !== 1) return;
    const y = e.touches[0].clientY;
    if (Math.abs(y - startY) >= SLOP_PX) moved = true;
    pushSample(y, e.timeStamp || performance.now());
  }

  function onTouchEnd(e) {
    if (!tracking) return;
    tracking = false;
    if (!moved) return;
    moved = false;
    velocity = currentVelocity((e && e.timeStamp) || performance.now());
    if (Math.abs(velocity) >= MIN_FLING_VELOCITY) startFling();
  }

  // Every listener is passive: this module never cancels or reorders
  // the events — it only observes them and animates afterwards.
  touchTarget.addEventListener('touchstart', onTouchStart, { passive: true });
  touchTarget.addEventListener('touchmove', onTouchMove, { passive: true });
  touchTarget.addEventListener('touchend', onTouchEnd, { passive: true });
  touchTarget.addEventListener('touchcancel', onTouchEnd, { passive: true });

  return {
    dispose: function () {
      cancelFling();
      touchTarget.removeEventListener('touchstart', onTouchStart);
      touchTarget.removeEventListener('touchmove', onTouchMove);
      touchTarget.removeEventListener('touchend', onTouchEnd);
      touchTarget.removeEventListener('touchcancel', onTouchEnd);
    },
  };
}
