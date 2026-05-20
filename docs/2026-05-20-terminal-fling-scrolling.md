# Touch-momentum (fling) scrolling for the phone terminal

**Issue:** #23 · **Date:** 2026-05-20

## What was done

The live terminal overlay now scrolls with **native iOS inertial
("fling") momentum** on the phone. Before this, a touch drag moved the
buffer 1:1 with the finger and stopped dead on release — reaching the
top of a long conversation took many small, effortful drags.

## How it works

xterm's scroll container, `.xterm-viewport`, is a genuine
`overflow-y:scroll` element with a full-height scroll area — iOS grants
it native momentum scrolling for free. The problem was *reaching* it:
`.xterm-screen` (the text layer) sits on top of the viewport and is
~20px wider-reaching, so a finger almost always lands on the text, not
the viewport. xterm then scrolled the viewport programmatically, with no
momentum.

`enableNativeTouchScroll()` in `terminal.js` hands the whole surface to
native scrolling, phone sessions only:

1. `.xterm-screen` is set to `pointer-events:none`, so every touch falls
   through to `.xterm-viewport` and iOS owns the gesture.
2. xterm's own touch handler (bound to the `.xterm` root) would still
   scroll programmatically *and* `preventDefault` — cancelling the
   native momentum. A capture-phase listener swallows the touch events
   (`stopPropagation`, never `preventDefault`) so xterm's handler never
   runs and iOS keeps the fling.
3. A stationary tap is detected and re-focuses the terminal, so the
   on-screen keyboard still opens (xterm's own tap-to-focus is gone with
   the text layer non-interactive).

The PC mirror window is untouched — it scrolls with a wheel and keeps
mouse text-selection.

### Path taken

The first attempt layered a hand-rolled JavaScript fling on top of
xterm. It was the wrong battle: phone testing showed the right ~20px
edge strip already flinging beautifully — that strip is bare
`.xterm-viewport`, getting *native* iOS momentum. A controlled DOM
measurement (`.xterm-screen` 398px vs `.xterm-viewport` 418px, viewport
`scrollHeight` 3510 ≫ `clientHeight` 660) confirmed it. The synthetic
fling was dropped entirely in favour of routing every touch to the
native scroller.

### Trade-off

With the text layer non-interactive, xterm's touch-based text selection
and link taps are gone on the phone. This was an accepted trade for
consistent native scrolling; selection can be revisited later.

## Files modified

- `app/webapp/static/terminal.js` — `enableNativeTouchScroll()`: sets
  `.xterm-screen` `pointer-events:none`, swallows xterm's root-level
  touch events in the capture phase, and re-focuses on a tap. Wired for
  non-mirror sessions, disposed on close.
- `tests/e2e/test_terminal_native_scroll.py` — **new** regression test
  (#23): opens a real terminal and asserts `.xterm-screen` is
  `pointer-events:none` and a centre hit-test resolves to
  `.xterm-viewport`.
- `app/webapp/static/terminal-momentum.js`, `tests/e2e/test_terminal_fling.py`
  — **removed** (the synthetic-fling approach and its test).
- `README.md`, `CLAUDE.md` — regression-net listing updated.

No CSS change — the text layer is made non-interactive in JS so the
rule never reaches the PC mirror window.

## Validation

- `scripts/verify-before-ship.ps1` — byte-compile + non-e2e pytest +
  Playwright e2e on Chromium and WebKit/iPhone. Exit 0.
- Native momentum itself can only be confirmed on a real iPhone — the
  e2e test pins the touch *routing* that makes it reachable.
