"""Regression pin for #1135: the wide desktop layout.

design.md Layout, "Wide layout" (fleet-config#968): at
``(min-width: 1100px) and (pointer: fine)`` the nav is an 80px left rail,
full height, each tab an icon over its label. At 1440px the old top control
sat in a 772px column while the Board went full-bleed beside it, so one
window mixed two measures. Now the Board starts at the rail's edge. Below
1100px, or on a coarse pointer at any width, nothing changes.

The Chromium projection is the desktop (fine pointer). The WebKit
projection is an iPhone (coarse pointer), so there it pins the other half of
the contract: no rail, whatever the width.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import stable_read

pytestmark = pytest.mark.smoke

RAIL = 80


def _box(page: Page, selector: str) -> dict:
    box = stable_read(lambda: page.locator(selector).bounding_box())
    assert box, f"{selector} has no layout box"
    return box


def test_wide_window_puts_the_nav_in_a_left_rail(
    authed_page: Page, base_url: str, browser_name: str
) -> None:
    if browser_name != "chromium":
        pytest.skip("fine-pointer layout; the WebKit projection is an iPhone")
    page = authed_page
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base_url}/", wait_until="domcontentloaded")

    nav = _box(page, "nav.tabs")
    assert nav["x"] == 0 and nav["y"] == 0, f"rail not pinned top-left: {nav}"
    assert nav["width"] == RAIL, f"rail is {nav['width']}px wide, not {RAIL}"
    assert nav["height"] == 900, f"rail is {nav['height']}px tall, not full height"

    last_y = -1.0
    for tab in page.locator("nav.tabs > .tab").all():
        expect(tab.locator(".tab-label")).to_be_visible()
        box = tab.bounding_box()
        assert box and box["y"] > last_y, "rail tabs are not stacked top to bottom"
        assert box["height"] >= 44, f"rail tab is {box['height']}px tall"
        icon = tab.locator(".tab-icon").bounding_box()
        label = tab.locator(".tab-label").bounding_box()
        assert icon and label and icon["y"] + icon["height"] <= label["y"] + 1, (
            "rail tab icon must sit over its label"
        )
        last_y = box["y"]

    app = _box(page, "#paneClaude")
    assert app["x"] >= RAIL, f"Code pane starts under the rail at x={app['x']}"

    # The Board spans from the rail's edge to the window's, header included.
    page.locator("#tabBoard").click()
    expect(page.locator("#paneBoard")).to_be_visible()
    width = page.evaluate("document.documentElement.clientWidth")
    board = _box(page, "#paneBoard")
    assert abs(board["x"] - RAIL) <= 1, f"Board starts at x={board['x']}, not the rail edge"
    assert board["x"] + board["width"] <= width + 1, "Board runs past the window"
    head = _box(page, "#paneBoard > .home-head")
    assert head["width"] > 772, "the Board header must span with the Board"


def test_below_1100px_keeps_the_top_control(
    authed_page: Page, base_url: str, browser_name: str
) -> None:
    if browser_name != "chromium":
        pytest.skip("fine-pointer layout; the WebKit projection is an iPhone")
    page = authed_page
    page.set_viewport_size({"width": 1099, "height": 900})
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    nav = _box(page, "nav.tabs")
    assert nav["x"] > 0 and nav["height"] < 100, f"top control became a rail: {nav}"


@pytest.mark.iphone
def test_coarse_pointer_never_gets_the_rail(
    authed_page: Page, base_url: str, browser_name: str
) -> None:
    if browser_name != "webkit":
        pytest.skip("coarse-pointer half; the WebKit projection is the iPhone")
    page = authed_page
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    nav = _box(page, "nav.tabs")
    assert nav["height"] < 100, f"a coarse pointer got the rail: {nav}"
