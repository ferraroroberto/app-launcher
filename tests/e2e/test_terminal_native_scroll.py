"""Regression pin for issue #23 — native touch-momentum scrolling.

The phone terminal relies on iOS native inertial scrolling of xterm's
`.xterm-viewport`. ``enableNativeTouchScroll()`` in
``terminal-touch.js`` makes that reachable: it sets `.xterm-screen`
(the text layer that otherwise intercepts touches) to
`pointer-events:none`, and swallows xterm's own root-level touch
events in the capture phase so xterm can't `preventDefault` and cancel
the native momentum.

This exercises that module directly on a standalone xterm — the real
integration path can't be reached here because the e2e harness
connects over loopback, which the app treats as the PC mirror (where
the feature is intentionally skipped). It pins:

* `.xterm-screen` becomes `pointer-events:none`.
* A touch event on a child is swallowed in the capture phase — it
  never reaches a document-level listener (so xterm's bubble-phase
  handler never runs either).
* A stationary tap re-focuses the terminal; a travelled touch does not.
* `dispose()` restores `.xterm-screen` and stops swallowing.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.smoke

# Builds a standalone xterm, wires the real module, and runs the probes.
# WebKit rejects `new Touch()` / `new TouchEvent()`, so touches are
# plain Events with expando touch lists — the module only reads
# touches[0]/changedTouches[0].clientX/Y and timeStamp.
_PROBE = r"""
async () => {
  const { enableNativeTouchScroll } = await import('/static/terminal-touch.js');

  const host = document.createElement('div');
  host.style.cssText = 'position:fixed;left:0;top:0;width:320px;height:240px;';
  document.body.appendChild(host);
  const term = new window.Terminal({ scrollback: 1000, fontSize: 13 });
  term.open(host);

  let focusCount = 0;
  const realFocus = term.focus.bind(term);
  term.focus = () => { focusCount++; realFocus(); };

  const screen = host.querySelector('.xterm-screen');
  const viewport = host.querySelector('.xterm-viewport');

  const dispose = enableNativeTouchScroll(term);
  const screenPE_wired = getComputedStyle(screen).pointerEvents;

  // A touch dispatched on a child must be swallowed before it reaches
  // the document (capture-phase stopPropagation on the xterm root).
  let docSawTouch = 0;
  const docListener = () => { docSawTouch++; };
  document.addEventListener('touchmove', docListener);

  const fire = (el, type, x, y) => {
    const pt = { identifier: 1, clientX: x, clientY: y, pageX: x, pageY: y };
    const live = type === 'touchend' ? [] : [pt];
    const ev = new Event(type, { bubbles: true, cancelable: true });
    ev.touches = live; ev.targetTouches = live; ev.changedTouches = [pt];
    el.dispatchEvent(ev);
  };

  fire(viewport, 'touchmove', 100, 100);
  const swallowedWhileWired = docSawTouch === 0;

  // Stationary tap → focus; a travelled touch → no focus.
  const focusBeforeTap = focusCount;
  fire(viewport, 'touchstart', 100, 100);
  fire(viewport, 'touchend', 101, 101);
  const tapFocused = focusCount > focusBeforeTap;

  const focusBeforeDrag = focusCount;
  fire(viewport, 'touchstart', 100, 100);
  fire(viewport, 'touchend', 100, 200);
  const dragFocused = focusCount > focusBeforeDrag;

  // After dispose: screen restored, touches no longer swallowed.
  dispose();
  const screenPE_disposed = getComputedStyle(screen).pointerEvents;
  docSawTouch = 0;
  fire(viewport, 'touchmove', 100, 100);
  const swallowedAfterDispose = docSawTouch === 0;

  document.removeEventListener('touchmove', docListener);
  host.remove();
  return {
    screenPE_wired, swallowedWhileWired, tapFocused, dragFocused,
    screenPE_disposed, swallowedAfterDispose,
  };
}
"""


def test_native_touch_scroll_routing(authed_page: Page, base_url: str) -> None:
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.wait_for_function(
        "() => typeof window.Terminal === 'function'", timeout=5_000
    )
    r = authed_page.evaluate(_PROBE)

    assert r["screenPE_wired"] == "none", (
        f".xterm-screen pointer-events is {r['screenPE_wired']!r} after wiring, "
        "expected 'none' — touches won't fall through to the native viewport"
    )
    assert r["swallowedWhileWired"], (
        "a touchmove on a child reached document — xterm's root touch handler "
        "is no longer swallowed, so it can preventDefault and kill native momentum"
    )
    assert r["tapFocused"], "a stationary tap did not re-focus the terminal"
    assert not r["dragFocused"], "a travelled touch (a scroll) wrongly re-focused"
    assert r["screenPE_disposed"] != "none", (
        f"dispose() left .xterm-screen pointer-events at {r['screenPE_disposed']!r}"
    )
    assert not r["swallowedAfterDispose"], (
        "touches still swallowed after dispose() — listeners were not removed"
    )
