/* Touch-momentum (fling) scrolling for the xterm.js viewport — issue #23.
 *
 * xterm virtualizes its scrollback, so iOS WebKit never grants the
 * `.xterm-viewport` element native inertial scrolling: from the
 * browser's view the element keeps "jumping" rather than smoothly
 * scrolling. This module layers a custom fling on top — it tracks the
 * finger, drives `viewport.scrollTop` directly (xterm re-renders from
 * the viewport's own `scroll` event), and on release animates a
 * decaying inertial scroll until it settles.
 *
 * Selection is preserved: a touch that stays within SLOP_PX is never
 * intercepted, so xterm still gets long-press text-select and
 * double-tap word-select. Only once the finger travels past the slop
 * do we claim the gesture as a pan and start calling preventDefault().
 *
 * Desktop is untouched — only `touch*` listeners are wired, so wheel
 * scrolling never reaches this code.
 */

// --- Tuning constants (expect 1-2 phone passes to settle) ------------
// Pixels the finger may travel before a touch is claimed as a pan
// rather than a tap/long-press handed to xterm for selection.
const SLOP_PX = 8;
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
// A reference frame at 60fps — used to keep FRICTION frame-rate
// independent when the display runs faster or slower.
const FRAME_MS = 1000 / 60;

/**
 * Wire custom touch-momentum scrolling onto an xterm `.xterm-viewport`.
 *
 * @param {HTMLElement} viewport  the `.xterm-viewport` scroll element.
 * @param {{onFlingState?: (active: boolean) => void}} [hooks]
 *        onFlingState fires true when an inertial scroll starts and
 *        false when it settles — the caller uses it to suspend
 *        tail-follow auto-scroll so new output can't fight the fling.
 * @returns {{dispose: () => void}}  removes every listener and cancels
 *        any in-flight fling animation.
 */
export function wireTouchMomentum(viewport, hooks) {
  let tracking = false;     // a single-finger touch is in progress
  let panning = false;      // that touch has passed the slop → we own it
  let startY = 0;           // clientY at touchstart (slop is measured here)
  let lastY = 0;            // clientY of the previous applied move
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
    panning = false;
    startY = lastY = e.touches[0].clientY;
    samples.length = 0;
    pushSample(startY, e.timeStamp || performance.now());
  }

  function onTouchMove(e) {
    if (!tracking || e.touches.length !== 1) return;
    const y = e.touches[0].clientY;
    const now = e.timeStamp || performance.now();
    // Still inside the slop: ambiguous between a tap/long-press and a
    // pan. Leave it for xterm so text selection keeps working.
    if (!panning && Math.abs(y - startY) < SLOP_PX) return;
    panning = true;
    // Non-passive listener: claim the gesture so iOS doesn't also
    // rubber-band or pan the page underneath.
    if (e.cancelable) e.preventDefault();
    viewport.scrollTop -= (y - lastY);
    lastY = y;
    pushSample(y, now);
  }

  function onTouchEnd(e) {
    if (!tracking) return;
    tracking = false;
    if (!panning) return;
    panning = false;
    velocity = currentVelocity((e && e.timeStamp) || performance.now());
    if (Math.abs(velocity) >= MIN_FLING_VELOCITY) startFling();
  }

  viewport.addEventListener('touchstart', onTouchStart, { passive: true });
  viewport.addEventListener('touchmove', onTouchMove, { passive: false });
  viewport.addEventListener('touchend', onTouchEnd, { passive: true });
  viewport.addEventListener('touchcancel', onTouchEnd, { passive: true });

  return {
    dispose: function () {
      cancelFling();
      viewport.removeEventListener('touchstart', onTouchStart);
      viewport.removeEventListener('touchmove', onTouchMove);
      viewport.removeEventListener('touchend', onTouchEnd);
      viewport.removeEventListener('touchcancel', onTouchEnd);
    },
  };
}
