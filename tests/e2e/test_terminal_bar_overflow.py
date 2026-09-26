"""Terminal-bar controls stay inside the viewport (issues #514, #981).

History: `.terminal-bar-actions` (then six of eight bar buttons) had no
`min-width: 0`, so as a flex item of the row-flex `.terminal-bar` it could
never shrink below its buttons' summed width, and #496's wider Back button
plus a Kill clearance margin pushed the last button past the screen edge,
where `.terminal-overlay { overflow: hidden }` silently clipped it. #514 gave
the group `min-width: 0` and an internal `overflow-x: auto` scroller and put
every bar button back on the uniform 44px target at a 6px gap.

#980 moved Paste, Image, Compose and Keys into the composer, and #981 moved
✕ Kill and ↓ Jump into the ⋮ session menu and a floating Latest pill, and
#982 put the icon-only Terminal ⇄ Chat toggle first in the actions group. The
session overlay's bar is now ‹ Back · title · Terminal⇄Chat · 🔊 · ⋮, so it
no longer needs the scroller at all: `#terminalOverlay .terminal-bar-actions`
is `overflow-x: visible` (the Life OS bars that share the class keep
scrolling, #886). Both tests assert the row — the two-segment toggle
included — fits without any scrolling: at 320px (iPhone SE 1st-gen, the
narrowest phone) and at the suite's default iPhone 15 Pro Max projection.

#1223 put the context ring first in the group, before the toggle. It only
draws with a number, so both tests stub its route with one (a stub-child
PTY paints no statusline) and measure the worst case: ring, toggle, 🔊 and
⋮ all showing, the ring a real 44px target like its neighbours.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import OVERLAY_OPEN_MS

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_NARROW_VIEWPORT = {"width": 320, "height": 640}


def _mock_context(page: Page, percent: int) -> None:
    """The ring's route (#1223) answering a known percentage."""
    page.route(
        re.compile(r".*/api/claude-code/sessions/[^/]+/context$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"available": True, "percent": percent, "reason": None}),
        ),
    )


def test_terminal_bar_buttons_stay_within_viewport(
    authed_page: Page, base_url: str, browser_name: str, launched_pty_session: str
) -> None:
    if browser_name != "webkit":
        pytest.skip("phone-width overflow only reproduces under the iPhone projection")

    authed_page.set_viewport_size(_NARROW_VIEWPORT)
    _mock_context(authed_page, 42)
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
    # past the screen edge. The context ring (#1223) draws once its stubbed
    # route has answered, and says what it shows.
    ring = authed_page.locator("#contextRing")
    expect(ring).to_be_visible()
    expect(ring).to_have_attribute("aria-label", "Context window 42% used")
    authed_page.evaluate("document.querySelector('#terminalSpeak').hidden = false")
    boxes = authed_page.eval_on_selector_all(
        "#terminalOverlay .terminal-bar-actions .term-btn, #sessionMode",
        "els => els.map(el => el.getBoundingClientRect())",
    )
    assert len(boxes) == 4, "expected the context ring, 🔊, ⋮ and the mode toggle in the group"
    for box in boxes:
        assert box["left"] >= 0, f"button left edge {box['left']} clipped before the viewport"
        assert box["right"] <= viewport_width, (
            f"button right edge {box['right']} overflows viewport width {viewport_width}"
        )

    # Back anchors the bar's left edge and ⋮ its right edge (#981); the
    # ring (#1223) and the mode toggle (#982) sit between them, on-screen.
    for selector in ("#terminalBack", "#contextRing", "#sessionMode", "#terminalMenu"):
        box = authed_page.eval_on_selector(selector, "el => el.getBoundingClientRect()")
        assert box["left"] >= 0
        assert box["right"] <= viewport_width

    # The bar is a cluster, so the ring's 44px is real geometry (design.md
    # touch targets), not an expanded hit area overlapping the toggle.
    ring_box = authed_page.eval_on_selector("#contextRing", "el => el.getBoundingClientRect()")
    toggle_box = authed_page.eval_on_selector("#sessionMode", "el => el.getBoundingClientRect()")
    assert ring_box["width"] >= 44 and ring_box["height"] >= 44, f"ring target {ring_box}"
    assert ring_box["right"] <= toggle_box["left"], "the ring overlaps the mode toggle"

    # A tap reads the number out.
    ring.click()
    expect(authed_page.locator("#toast")).to_contain_text("Context window 42% used")


def test_terminal_bar_fits_at_once_on_default_phone(
    authed_page: Page, base_url: str, browser_name: str, launched_pty_session: str
) -> None:
    """Every bar button fits the default iPhone projection without scrolling.

    The second half of #514: with the Back button back at the uniform 44px and
    uniform 6px gaps, the full row — including the read-aloud button, hidden by
    default and force-shown here to measure the worst case — must be fully
    visible at once on the suite's default iPhone 15 Pro Max (430px) viewport,
    with no internal scrolling and no clipped button. Five controls since
    #1223 (Back, the context ring, the Terminal⇄Chat toggle, Read aloud,
    ⋮ menu).
    """
    if browser_name != "webkit":
        pytest.skip("phone-width row-fit only meaningful under the iPhone projection")

    _mock_context(authed_page, 87)
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
    # visible — unhide it so the measurement covers every action button —
    # and the context ring (#1223) is drawn, here at a danger-tier 87%.
    ring = authed_page.locator("#contextRing")
    expect(ring).to_be_visible()
    expect(ring).to_have_attribute("data-tier", "danger")
    authed_page.evaluate("document.querySelector('#terminalSpeak').hidden = false")

    # Equal-size contract: the Back button is the same 44px target as every
    # other bar button (the #496 64px widening is what tipped the row over),
    # the ring included.
    widths = authed_page.evaluate(
        "() => ['#terminalBack', '#contextRing', '#terminalSpeak', '#terminalMenu']"
        ".map(s => document.querySelector(s).getBoundingClientRect().width)"
    )
    assert max(widths) - min(widths) <= 1, f"bar buttons unequal widths: {widths}"
    assert min(widths) >= 44, f"a bar button under the 44px target: {widths}"
    # The toggle and the ring are the same 44px tall as their neighbours
    # (#982, #1223); the ring leads the group, then the toggle, then 🔊.
    toggle = authed_page.eval_on_selector("#sessionMode", "el => el.getBoundingClientRect()")
    assert abs(toggle["height"] - widths[0]) <= 1, f"toggle height {toggle['height']} vs 44px controls"
    ring_box = authed_page.eval_on_selector("#contextRing", "el => el.getBoundingClientRect()")
    assert abs(ring_box["height"] - widths[0]) <= 1, f"ring height {ring_box['height']} vs 44px controls"
    order = authed_page.eval_on_selector_all(
        "#terminalOverlay .terminal-bar-actions > *", "els => els.map(el => el.id)"
    )
    assert order[:3] == ["contextRing", "sessionMode", "terminalSpeak"], order

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
    assert len(visible) == 4, (
        f"expected all 4 bar buttons visible, hidden: "
        f"{[b['id'] for b in buttons if b['hidden']]}"
    )
    for b in visible:
        assert b["box"]["left"] >= 0, f"{b['id']} left edge {b['box']['left']} clipped"
        assert b["box"]["right"] <= viewport_width, (
            f"{b['id']} right edge {b['box']['right']} overflows viewport {viewport_width}"
        )
