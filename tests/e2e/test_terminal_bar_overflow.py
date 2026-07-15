"""Terminal-bar action buttons stay inside the viewport (issue #514).

Regression for a same-day double regression: `.terminal-bar-actions` (six of
the eight terminal-bar buttons) had no `min-width: 0`, so as a flex item of
the row-flex `.terminal-bar` its automatic minimum size was its content's
min-content width — it could never shrink below the sum of its buttons. That
ceiling was already tight on a narrow phone; #496 (widening `#terminalBack`
56px -> 64px + `#terminalKill` margin) tipped it further, and
`.terminal-overlay { overflow: hidden }` silently clipped the last button off
the screen instead of showing it.

The fix has two halves:

- `.terminal-bar-actions` gets `min-width: 0` plus its own `overflow-x: auto`
  scroller (same pattern as `.board-columns`), so a too-narrow bar scrolls
  internally within its own padding instead of bleeding past the viewport
  edge (safety net for the narrowest phones).
- #496's Back-button widening (64px) and Kill clearance margin are reverted:
  every bar button is the uniform 44px HIG target at a uniform 6px gap, so
  the full eight-button row (420px) genuinely fits a 430px phone viewport at
  once — no scrolling needed on the default projection.

The narrow-viewport test uses 320px (iPhone SE 1st-gen width) because that is
where the scroller safety net actually engages; the fits-at-once test runs on
the suite's default iPhone 15 Pro Max (430px) projection where the whole row
must be visible without scrolling.
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


def test_terminal_bar_fits_at_once_on_default_phone(
    authed_page: Page, base_url: str, browser_name: str, launched_pty_session: str
) -> None:
    """All eight bar buttons fit the default iPhone projection without scrolling.

    The second half of #514: with the Back button back at the uniform 44px and
    uniform 6px gaps, the full row — including the read-aloud button, hidden by
    default and force-shown here to measure the worst case — must be fully
    visible at once on the suite's default iPhone 15 Pro Max (430px) viewport,
    with no internal scrolling and no clipped button.
    """
    if browser_name != "webkit":
        pytest.skip("phone-width row-fit only meaningful under the iPhone projection")

    # Open via the session-list row tap — the phone path. The ?terminal= deep
    # link would classify this loopback open as a PC mirror window (#241) and
    # hide the compose button, undercounting the row's real phone width.
    authed_page.goto(base_url, wait_until="domcontentloaded")
    pty_row = authed_page.locator(
        f'#sessionsList li.session-item[data-session-id="{launched_pty_session}"]'
    )
    pty_row.locator(".session-open").click()
    authed_page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=10_000)

    viewport_width = authed_page.evaluate("window.innerWidth")

    # Worst case is a Claude session where the 🔊 read-aloud button (#190) is
    # visible — unhide it so the measurement covers all six action buttons.
    authed_page.evaluate("document.querySelector('#terminalSpeak').hidden = false")

    # Equal-size contract: the Back button is the same 44px target as every
    # other bar button (the #496 64px widening is what tipped the row over).
    widths = authed_page.evaluate(
        "() => ['#terminalBack', '#terminalKill', '#terminalKeys']"
        ".map(s => document.querySelector(s).getBoundingClientRect().width)"
    )
    assert max(widths) - min(widths) <= 1, f"bar buttons unequal widths: {widths}"

    # The actions group must not need its scroller on this width…
    group = authed_page.eval_on_selector(
        "#terminalOverlay .terminal-bar-actions",
        "g => ({scrollWidth: g.scrollWidth, clientWidth: g.clientWidth})",
    )
    assert group["scrollWidth"] <= group["clientWidth"] + 1, (
        f"actions group scrolls on the default phone width: {group}"
    )

    # …and every button — without any scrolling — sits fully on-screen.
    buttons = authed_page.eval_on_selector_all(
        "#terminalOverlay .terminal-bar .term-btn",
        "els => els.map(el => ({id: el.id, hidden: el.hidden,"
        " box: el.getBoundingClientRect()}))",
    )
    visible = [b for b in buttons if not b["hidden"]]
    assert len(visible) == 8, (
        f"expected all 8 bar buttons visible, hidden: "
        f"{[b['id'] for b in buttons if b['hidden']]}"
    )
    for b in visible:
        assert b["box"]["left"] >= 0, f"{b['id']} left edge {b['box']['left']} clipped"
        assert b["box"]["right"] <= viewport_width, (
            f"{b['id']} right edge {b['box']['right']} overflows viewport {viewport_width}"
        )
