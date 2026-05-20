# Touch-momentum (fling) scrolling for the phone terminal

**Issue:** #23 · **Date:** 2026-05-20

## What was done

The live terminal overlay now has inertial ("fling") touch scrolling on
the phone. Before this, every drag on the terminal moved the buffer 1:1
with the finger and stopped dead the instant the finger lifted — getting
from the latest reply back to the top of a long conversation took many
small, effortful drags.

## How xterm scrolls, and what was missing

xterm.js already handles a touch *drag*: it binds `touchstart` /
`touchmove` to its root element (`.xterm`) and tracks the finger 1:1
into `.xterm-viewport.scrollTop`. What it never does is *fling* — there
is no `touchend` inertia.

`terminal-momentum.js` layers only that missing piece on top. It is
**purely additive**: a passive observer on the same root element xterm
uses — it never calls `preventDefault` or `stopPropagation`, so xterm
keeps full ownership of the drag and all of its selection behaviour
(long-press, double-tap) is untouched. The module tracks the finger's
velocity during the drag and, on `touchend`, animates a decaying
inertial scroll of `.xterm-viewport`.

### A bug caught in phone testing

The first cut wired the handler onto `.xterm-viewport` and also drove
the scroll itself. That was dead code: `.xterm-viewport` is a *sibling*
of `.xterm-screen` (the layer the finger actually touches), so touch
events bubble `screen → .xterm → document` and never reach the
viewport. The fix binds to the `.xterm` root (`term.element`) — the
element xterm itself binds touch to — and stays a passive observer.

### Behaviour

- A fast swipe glides on after release, decelerating naturally
  (per-frame velocity decay) until it settles or hits an edge.
- A finger held still before lifting (last move older than the 100 ms
  velocity window) yields zero release velocity — a deliberate
  reposition, no overshoot.
- Selection is untouched — the module never intercepts events, so
  xterm's long-press / double-tap behaviour is exactly as before.
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

- `app/webapp/static/terminal-momentum.js` — **new.**
  `wireTouchMomentum(touchTarget, viewport, hooks)` exports the momentum
  handler; observes the finger via passive `touch*` listeners and
  animates `viewport.scrollTop` with a decaying inertial scroll on
  release.
- `app/webapp/static/terminal.js` — wires the handler onto
  `term.element` for non-mirror sessions, disposes it on close, and
  suspends tail-follow (`!t.flinging`) while a fling is in flight.
- `tests/e2e/test_terminal_fling.py` — **new** regression test (#23):
  builds a standalone xterm, wires the real momentum module onto its
  root element, and asserts a fast swipe keeps scrolling after
  `touchend` while a paused release does not. Engine-agnostic — drives
  the handler with plain `Event` objects carrying expando touch lists,
  since WebKit rejects `new Touch()` / `new TouchEvent()`.
- `README.md`, `CLAUDE.md` — added the new test to the iPhone
  regression-net listing.

No CSS change was needed — xterm's own per-drag scroll already works;
this only adds the release inertia.

## Validation

- `scripts/verify-before-ship.ps1` — byte-compile + non-e2e pytest (65
  passed) + Playwright e2e on Chromium and WebKit/iPhone (29 passed, 7
  skipped). Exit 0.
- Deceleration feel still wants a real-device pass to settle the tuning
  constants.
