"""Regression pin for issue #23 — touch-momentum (fling) scrolling.

xterm's viewport never gets native iOS inertia because the scrollback is
virtualized; ``terminal-momentum.js`` layers a custom fling on top. This
test exercises that module directly: it builds a standalone xterm in the
page, writes enough scrollback to be scrollable, wires the real momentum
handler onto its ``.xterm-viewport``, and dispatches synthetic touch
gestures.

Two properties are pinned:

* A fast swipe keeps scrolling **after** ``touchend`` (inertia) and then
  settles — a broken handler would stop dead on release.
* A slow drag (samples spaced past the velocity window) does **not**
  fling — it must stop where the finger left it, so a deliberate
  reposition isn't overshot.

Building a throwaway Terminal keeps the test deterministic and free of a
live ``claude`` PTY: the unit under test only needs a scrollable
``.xterm-viewport``, not real session output.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.smoke

# JS gesture harness. Builds a standalone xterm, wires the real momentum
# module, runs one touch gesture, and reports the scroll position the
# instant the finger lifts plus a frame-by-frame trace of what happened
# afterwards. The gesture is always the same fast 8-step swipe; `pauseMs`
# is dead time held still between the last move and touchend — past
# VELOCITY_WINDOW_MS it rounds the release velocity to zero (a paused
# reposition, no fling).
_RUN_GESTURE = """
async ({ pauseMs }) => {
  const { wireTouchMomentum } = await import('/static/terminal-momentum.js');

  const host = document.createElement('div');
  host.style.cssText =
    'position:fixed;left:0;top:0;width:320px;height:240px;z-index:9999;';
  document.body.appendChild(host);

  const term = new window.Terminal({ scrollback: 10000, fontSize: 13 });
  term.open(host);
  let lines = '';
  for (let i = 0; i < 600; i++) lines += 'fling-line ' + i + '\\r\\n';
  await new Promise((res) => term.write(lines, res));
  // xterm sizes the scroll area on a render frame after write resolves —
  // wait two frames so .xterm-viewport.scrollHeight reflects all 600 rows.
  await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));

  const viewport = host.querySelector('.xterm-viewport');
  // Start pinned at the tail so a downward swipe has the whole
  // scrollback to glide into.
  viewport.scrollTop = viewport.scrollHeight;
  const momentum = wireTouchMomentum(viewport, {});

  // WebKit refuses `new Touch()` / `new TouchEvent()` ("Illegal
  // constructor"). The momentum handler only reads touches[0].clientY,
  // touches.length, timeStamp and cancelable, so a plain Event of the
  // right type with expando touch lists drives it on every engine.
  const fire = (type, y) => {
    const pt = { identifier: 1, clientX: 100, clientY: y, pageX: 100, pageY: y };
    const live = type === 'touchend' ? [] : [pt];
    const ev = new Event(type, { bubbles: true, cancelable: true });
    ev.touches = live;
    ev.targetTouches = live;
    ev.changedTouches = [pt];
    viewport.dispatchEvent(ev);
  };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const frame = () => new Promise((r) => requestAnimationFrame(r));

  // Swipe the finger downwards (clientY increasing) — that reveals
  // earlier history, i.e. scrollTop decreases.
  let y = 40;
  fire('touchstart', y);
  for (let i = 0; i < 8; i++) {
    await sleep(16);
    y += 22;
    fire('touchmove', y);
  }
  if (pauseMs > 0) await sleep(pauseMs);
  fire('touchend', y);
  const atRelease = viewport.scrollTop;

  // Trace frame-by-frame until the scroll settles (two near-identical
  // frames) or a generous cap — a real fling decays over many frames,
  // so a fixed tiny window would clip a still-moving glide.
  const trace = [];
  let prev = atRelease;
  let settled = false;
  for (let i = 0; i < 240; i++) {
    await frame();
    const top = viewport.scrollTop;
    trace.push(top);
    if (i > 1 && Math.abs(top - prev) < 0.5) { settled = true; break; }
    prev = top;
  }

  momentum.dispose();
  host.remove();
  return { atRelease, trace, settled };
}
"""


def _navigate(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    # The momentum module needs window.Terminal — wait for the xterm
    # vendor script to have run.
    page.wait_for_function("() => typeof window.Terminal === 'function'", timeout=5_000)


def test_fast_swipe_glides_after_release(authed_page: Page, base_url: str) -> None:
    _navigate(authed_page, base_url)
    result = authed_page.evaluate(_RUN_GESTURE, {"pauseMs": 0})

    at_release = result["atRelease"]
    trace = result["trace"]
    final = trace[-1]

    # Inertia: scrollTop must keep decreasing well past where the finger
    # lifted — a handler that stopped dead on touchend would leave the
    # whole trace equal to `at_release`.
    assert final < at_release - 20, (
        f"no inertial glide: scrollTop {at_release} at release, {final} after "
        f"{len(trace)} frames — fling handler stopped on touchend"
    )
    # And it must settle within the cap, not run forever — the decay
    # has to round the velocity down to a stop.
    assert result["settled"], (
        f"fling never settled within 240 frames — tail still moving: {trace[-4:]}"
    )


def test_paused_release_does_not_fling(authed_page: Page, base_url: str) -> None:
    _navigate(authed_page, base_url)
    # Same fast swipe, but the finger is held still for 180 ms before
    # lifting — past VELOCITY_WINDOW_MS (100 ms), so the release velocity
    # rounds to zero and the scroll must stop where the finger left it.
    result = authed_page.evaluate(_RUN_GESTURE, {"pauseMs": 180})

    at_release = result["atRelease"]
    final = result["trace"][-1]
    assert abs(final - at_release) < 2, (
        f"paused release flung anyway: scrollTop {at_release} at release drifted "
        f"to {final} — momentum fired without a live flick velocity"
    )
