"""Terminal-bar action buttons stay inside the viewport (issue #514).

Regression for a same-day double regression: `.terminal-bar-actions` (six of
the eight terminal-bar buttons) had no `min-width: 0`, so as a flex item of
the row-flex `.terminal-bar` its automatic minimum size was its content's
min-content width — it could never shrink below the sum of its buttons. That
ceiling was already tight on a narrow phone; #496 (widening `#terminalBack`
56px -> 64px + `#terminalKill` margin) tipped it further, and
`.terminal-overlay { overflow: hidden }` silently clipped the last button off
the screen instead of showing it.

The fix gives `.terminal-bar-actions` `min-width: 0` plus its own
`overflow-x: auto` scroller (same pattern as `.board-columns`), so a
too-narrow bar scrolls internally within its own padding instead of bleeding
past the viewport edge.

Uses an explicit narrow viewport (320px, iPhone SE 1st-gen width) rather than
the suite's default iPhone 15 Pro Max (430px) projection: at 430px the eight
buttons already fit without the fix reproducing the overflow at all — the
narrower width is what actually exercises the bug (and matches "smallest
supported phone" rather than the widest).
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.smoke

_NARROW_VIEWPORT = {"width": 320, "height": 640}


def test_terminal_bar_buttons_stay_within_viewport(
    authed_page: Page, base_url: str, browser_name: str, launched_pty_session: str
) -> None:
    if browser_name != "webkit":
        pytest.skip("phone-width overflow only reproduces under the iPhone projection")

    authed_page.set_viewport_size(_NARROW_VIEWPORT)
    sid = launched_pty_session
    authed_page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")
    authed_page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=10_000)

    viewport_width = authed_page.evaluate("window.innerWidth")
    assert viewport_width == _NARROW_VIEWPORT["width"]

    # .terminal-bar / .terminal-bar-actions are shared classes with the Life
    # OS doc-browser bar (#lifeOsBrowser) — scope to #terminalOverlay so the
    # measurement targets the actual open terminal, not the other (hidden,
    # zero-size) bar sharing the same class names.
    #
    # Scroll the actions group as far right as it goes, then confirm every
    # button in it lands fully inside the viewport — i.e. reachable, not
    # clipped unreachable past the screen edge.
    authed_page.evaluate(
        "() => { const g = document.querySelector('#terminalOverlay .terminal-bar-actions');"
        " g.scrollLeft = g.scrollWidth; }"
    )
    boxes = authed_page.eval_on_selector_all(
        "#terminalOverlay .terminal-bar-actions .term-btn",
        "els => els.map(el => el.getBoundingClientRect())",
    )
    assert boxes, "expected terminal-bar-actions buttons to be present"
    for box in boxes:
        assert box["left"] >= 0, f"button left edge {box['left']} clipped before the viewport"
        assert box["right"] <= viewport_width, (
            f"button right edge {box['right']} overflows viewport width {viewport_width}"
        )

    # The always-visible Back/Kill pair (outside the scroller) must also stay
    # on-screen — they anchor the bar's left edge.
    for selector in ("#terminalBack", "#terminalKill"):
        box = authed_page.eval_on_selector(selector, "el => el.getBoundingClientRect()")
        assert box["left"] >= 0
        assert box["right"] <= viewport_width
