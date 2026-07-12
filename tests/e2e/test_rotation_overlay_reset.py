"""Regression pin for issue #446 (rotation leaves the overlay undersized).

A portrait -> landscape -> portrait rotation cycle can leave
``window.innerHeight`` and ``visualViewport.height`` transiently
mismatched while iOS's chrome bars settle. ``applySize()``'s keyboard
heuristic (``keyboardOverlayHeight()``) can sample that mismatch mid-
transition on a stray ``resize``/``visualViewport resize`` event and pin
the terminal overlay to a stale shrunk height/top — with no guaranteed
later event to release it, leaving the Coding tab's session list and
Projects grid bleeding through in the gap below the terminal.

The fix wires an ``orientationchange`` listener (``terminal.js``,
``openTerminal()``) that fires once per rotation regardless of that
race: it immediately releases any pin back to the CSS ``100dvh`` default,
then re-runs ``applySize()`` after a short settle delay so a keyboard
that's genuinely still open gets correctly re-pinned.

The keyboard itself can't be raised in a headless browser, so this pins
the release half of the contract against a real, live terminal instance
(not just the pure ``keyboardOverlayHeight()`` helper, which is already
covered by ``test_keyboard_overlay.py`` and is unchanged by this fix):
stash a stale pinned height/top on the overlay (simulating the stuck
state), dispatch a real ``orientationchange``, and assert the pin is
cleared synchronously — before the settle timer's re-fit even runs.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

# The SPA loads its modules cache-busted (`terminal.js?v=<asset_hash>`); see
# test_warm_terminal_reopen.py for why a plain import() would test a second,
# empty module instance instead of the live one driving the open overlay.
_LIVE_MODULE = """
(name) => {
  const hit = performance.getEntriesByType('resource')
    .map((r) => r.name)
    .find((n) => n.includes('/static/' + name + '?v='));
  return hit || ('/static/' + name);
}
"""


def test_orientationchange_releases_stale_overlay_pin(
    authed_page: Page,
    base_url: str,
    launched_pty_session: str,
) -> None:
    sid = launched_pty_session
    # NOT the ?terminal=<sid> deep-link: that flags state.isMirrorWindow,
    # which routes openTerminal() through the PC-mirror branch — the
    # overlay-chrome pinning (and the orientationchange listener this test
    # pins) never runs there. Open the same way a real phone tap does:
    # normal page load, then openTerminal() directly, matching
    # test_warm_terminal_reopen.py's re-open step.
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.evaluate(
        """async (sid) => {
          const live = """ + _LIVE_MODULE + """;
          const term = await import(live('terminal.js'));
          const { state } = await import(live('state.js'));
          const s = (state.sessions || []).find(
            (x) => x.session_id === sid
          ) || { session_id: sid, name: 'rotation-reset' };
          await term.openTerminal(s);
        }""",
        sid,
    )
    expect(authed_page.locator("#terminalOverlay")).to_be_visible()
    authed_page.wait_for_selector("#terminalHost .xterm", timeout=10_000)

    # Stage the stuck state and dispatch the rotation in ONE synchronous JS
    # call — a live terminal's own applySize() runs on 'resize'/visualViewport
    # events too, so splitting stage/dispatch across separate evaluate()
    # round-trips would race a real (unrelated) applySize() invocation
    # against the test's own staged pin. Everything inside a single
    # synchronous function body runs on one tick with no interleaving.
    result = authed_page.evaluate(
        """() => {
          const ov = document.getElementById('terminalOverlay');
          // Simulate the stuck state a mid-rotation resize race can leave
          // behind: the overlay pinned to a shrunk height/top as if the
          // keyboard heuristic had fired on a transient
          // window.innerHeight/visualViewport.height mismatch.
          ov.style.height = '300px';
          ov.style.bottom = 'auto';
          ov.style.top = '40px';
          const stuck = ov.style.height;
          // A real orientationchange must release the pin immediately,
          // before the settle-timer re-fit (350ms) even runs.
          window.dispatchEvent(new Event('orientationchange'));
          return {
            stuck,
            released: {
              height: ov.style.height,
              bottom: ov.style.bottom,
              top: ov.style.top,
            },
          };
        }"""
    )
    assert result["stuck"] == "300px", "test setup failed to stage the stale pin"
    released = result["released"]
    assert released == {"height": "", "bottom": "", "top": ""}, (
        f"overlay inline styles after orientationchange were {released!r}, "
        "expected all cleared ('') — a rotation left the overlay pinned to "
        "the stale shrunk size, exposing the page underneath it"
    )

    # No real keyboard is open in a headless run, so once the settle timer's
    # re-fit runs, applySize() must find no override needed and leave the
    # overlay released (not re-pin it from stale/empty visualViewport data).
    authed_page.wait_for_timeout(500)
    settled = authed_page.evaluate(
        """() => {
          const ov = document.getElementById('terminalOverlay');
          return { height: ov.style.height, bottom: ov.style.bottom, top: ov.style.top };
        }"""
    )
    assert settled == {"height": "", "bottom": "", "top": ""}, (
        f"overlay inline styles after the settle re-fit were {settled!r}, "
        "expected all still cleared — applySize() re-pinned the overlay "
        "with no keyboard actually open"
    )
