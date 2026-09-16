"""Terminal-bar controls stay inside the viewport (issues #514, #981).

History: `.terminal-bar-actions` (then six of eight bar buttons) had no
`min-width: 0`, so as a flex item of the row-flex `.terminal-bar` it could
never shrink below its buttons' summed width, and #496's wider Back button
plus a Kill clearance margin pushed the last button past the screen edge,
where `.terminal-overlay { overflow: hidden }` silently clipped it. #514 gave
the group `min-width: 0` and an internal `overflow-x: auto` scroller and put
every bar button back on the uniform 44px target at a 6px gap.

#980 moved Paste, Image, Compose and Keys into the composer, and #981 moved
✕ Kill and ↓ Jump into the ⋮ session menu and a floating Latest pill. The
session overlay's bar is now ‹ Back · title · [mode toggle, #982] · 🔊 · ⋮,
so it no longer needs the scroller at all: `#terminalOverlay
.terminal-bar-actions` is `overflow-x: visible` (the Life OS bars that share
the class keep scrolling, #886). Both tests assert the row fits without any
scrolling — at 320px (iPhone SE 1st-gen, the narrowest phone) and at the
suite's default iPhone 15 Pro Max projection.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

from tests.e2e.conftest import OVERLAY_OPEN_MS

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
    authed_page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)

    viewport_width = authed_page.evaluate("window.innerWidth")
    assert viewport_width == _NARROW_VIEWPORT["width"]

    # .terminal-bar / .terminal-bar-actions are shared classes with the Life
    # OS doc-browser bar (#lifeOsBrowser) — scope to #terminalOverlay so the
    # measurement targets the actual open terminal, not the other (hidden,
    # zero-size) bar sharing the same class names.
    #
    # Force 🔊 visible (the worst case), then confirm every button lands
    # fully inside the viewport with no scrolling — reachable, not clipped
    # past the screen edge.
    authed_page.evaluate("document.querySelector('#terminalSpeak').hidden = false")
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

    # Back anchors the bar's left edge and ⋮ its right edge (#981).
    for selector in ("#terminalBack", "#terminalMenu"):
        box = authed_page.eval_on_selector(selector, "el => el.getBoundingClientRect()")
        assert box["left"] >= 0
        assert box["right"] <= viewport_width


def test_terminal_bar_fits_at_once_on_default_phone(
    authed_page: Page, base_url: str, browser_name: str, launched_pty_session: str
) -> None:
    """Every bar button fits the default iPhone projection without scrolling.

    The second half of #514: with the Back button back at the uniform 44px and
    uniform 6px gaps, the full row — including the read-aloud button, hidden by
    default and force-shown here to measure the worst case — must be fully
    visible at once on the suite's default iPhone 15 Pro Max (430px) viewport,
    with no internal scrolling and no clipped button. Three buttons since #981
    (Back, Read aloud, ⋮ menu); #982 adds the mode toggle to the same group.
    """
    if browser_name != "webkit":
        pytest.skip("phone-width row-fit only meaningful under the iPhone projection")

    # Open via the session-list row tap — the phone path. The ?terminal= deep
    # link would classify this loopback open as a PC mirror window (#241) and
    # hide the composer, undercounting the row's real phone width.
    authed_page.goto(base_url, wait_until="domcontentloaded")
    pty_row = authed_page.locator(
        f'#sessionsList li.session-item[data-session-id="{launched_pty_session}"]'
    )
    pty_row.locator(".session-open").click()
    authed_page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)

    viewport_width = authed_page.evaluate("window.innerWidth")

    # Worst case is a Claude session where the 🔊 read-aloud button (#190) is
    # visible — unhide it so the measurement covers every action button.
    authed_page.evaluate("document.querySelector('#terminalSpeak').hidden = false")

    # Equal-size contract: the Back button is the same 44px target as every
    # other bar button (the #496 64px widening is what tipped the row over).
    widths = authed_page.evaluate(
        "() => ['#terminalBack', '#terminalSpeak', '#terminalMenu']"
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
    assert len(visible) == 3, (
        f"expected all 3 bar buttons visible, hidden: "
        f"{[b['id'] for b in buttons if b['hidden']]}"
    )
    for b in visible:
        assert b["box"]["left"] >= 0, f"{b['id']} left edge {b['box']['left']} clipped"
        assert b["box"]["right"] <= viewport_width, (
            f"{b['id']} right edge {b['box']['right']} overflows viewport {viewport_width}"
        )
