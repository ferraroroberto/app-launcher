# Touch-momentum (fling) scrolling for the phone terminal

**Issue:** #23 · **Date:** 2026-05-20

## What was done

The live terminal overlay now has inertial ("fling") touch scrolling on
the phone. Before this, every drag on the xterm.js viewport moved a few
lines and stopped dead the instant the finger lifted — getting from the
latest reply back to the top of a long conversation took many small,
effortful drags.

xterm.js virtualizes its scrollback, so iOS WebKit never grants the
`.xterm-viewport` element native momentum: from the browser's point of
view the element keeps "jumping" rather than smoothly scrolling. The fix
layers a custom momentum handler on top.

### Behaviour

- A fast swipe glides on after release, decelerating naturally (per-frame
  velocity decay) until it settles or hits an edge.
- A touch that stays within an 8 px slop is never intercepted, so xterm
  still gets long-press text-select and double-tap word-select.
- A finger held still before lifting (last move older than the 100 ms
  velocity window) yields zero release velocity — a deliberate
  reposition, no overshoot.
- Tail-follow auto-scroll yields to an active fling: new output arriving
  mid-glide no longer snaps the viewport to the bottom and fights the
  inertia. Follow re-engages once the fling settles at the tail.
- Desktop is untouched — only `touch*` listeners are wired, so wheel
  scrolling never reaches the new code. The PC mirror window is skipped
  entirely (no touch input).

The deceleration/velocity constants (`FRICTION`, `MIN_FLING_VELOCITY`,
`SLOP_PX`, `VELOCITY_WINDOW_MS`) are grouped at the top of the new module
and may want one phone-tuning pass — they are subjective by nature.

## Files modified

- `app/webapp/static/terminal-momentum.js` — **new.** `wireTouchMomentum()`
  exports the momentum handler; tracks the finger, drives
  `viewport.scrollTop` directly (xterm re-renders from its own `scroll`
  event), and animates a decaying inertial scroll on release.
- `app/webapp/static/terminal.js` — wires the handler onto the
  `.xterm-viewport` for non-mirror sessions, disposes it on close, and
  suspends tail-follow (`!t.flinging`) while a fling is in flight.
- `app/webapp/static/styles.css` — `touch-action: pan-y` on
  `.xterm-viewport`; refreshed the stale native-momentum comment.
- `tests/e2e/test_terminal_fling.py` — **new** regression test (#23):
  builds a standalone xterm, wires the real momentum module, and asserts
  a fast swipe keeps scrolling after `touchend` while a paused release
  does not. Engine-agnostic — drives the handler with plain `Event`
  objects carrying expando touch lists, since WebKit rejects
  `new Touch()` / `new TouchEvent()`.
- `README.md`, `CLAUDE.md` — added the new test to the iPhone
  regression-net listing.

## Validation

- `scripts/verify-before-ship.ps1` — byte-compile + non-e2e pytest (65
  passed) + Playwright e2e on Chromium and WebKit/iPhone (29 passed, 7
  skipped). Exit 0.
- Deceleration feel still wants a real-device pass to settle the tuning
  constants.
