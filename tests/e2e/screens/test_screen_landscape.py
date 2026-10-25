"""Regression pin for #1601: the Screen view on a phone held sideways.

In landscape on a phone (coarse pointer, short window) the title bar and the
tools row leave the flow behind a floating handle, and the picture takes the
height they held. The handle names the mode, and brings the bar and tools row
back as an overlay on top of the picture, which neither moves nor re-fits.
Portrait, a tablet, and the desktop pop-out keep the bars.

Every test here runs in a landscape touch window (the ``browser_context_args``
override below); the stub frame source is the one ``test_screen_view`` uses.
Safe-area insets can't be emulated in a desktop engine, so the notch clearance
is an on-device check.
"""
from __future__ import annotations

from typing import Dict

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.waits import wait_until
from tests.e2e.screens.test_screen_view import (
    _live_view,
    _point,
    _pointer,
    _set_mode,
    _tap,
)

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_LANDSCAPE = {"width": 844, "height": 390}
_PORTRAIT = {"width": 390, "height": 844}


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict) -> dict:
    """A phone on its side: a touch screen in a short, wide window. Overrides
    the suite's args for this module only (WebKit's iPhone projection is
    portrait; Chromium's default window has a fine pointer)."""
    return {
        **browser_context_args,
        "viewport": dict(_LANDSCAPE),
        "has_touch": True,
        "is_mobile": True,
    }


_GEOMETRY_JS = """() => {
  const box = (id) => {
    const r = document.getElementById(id).getBoundingClientRect();
    return { x: r.left, y: r.top, w: r.width, h: r.height };
  };
  return { vw: innerWidth, vh: innerHeight, stage: box('screenStage'),
           image: box('screenImage'), handle: box('screenHandle') };
}"""


def _geometry(page: Page) -> Dict:
    return page.evaluate(_GEOMETRY_JS)


def test_landscape_hides_the_bars_behind_a_handle_that_brings_them_back(
    authed_page: Page, base_url: str
) -> None:
    seen, page = _live_view(authed_page, base_url)
    handle = page.locator("#screenHandle")
    bar = page.locator(".screen-view-bar")
    tools = page.locator(".screen-view-tools")

    # The bars are gone, the handle is there, and the picture has the height.
    expect(handle).to_be_visible()
    expect(bar).to_be_hidden()
    expect(tools).to_be_hidden()
    expect(handle).to_have_attribute("aria-expanded", "false")
    expect(handle).to_contain_text("View")
    closed = _geometry(page)
    assert abs(closed["stage"]["h"] - closed["vh"]) <= 1, closed
    assert abs(closed["stage"]["w"] - closed["vw"]) <= 1, closed
    assert closed["handle"]["h"] >= 44, closed["handle"]
    assert closed["handle"]["x"] >= 0 and closed["handle"]["y"] >= 0, closed["handle"]

    # The handle brings the bar and tools row back over the picture, which does
    # not move, resize or re-fit.
    handle.click()
    expect(handle).to_have_attribute("aria-expanded", "true")
    for part in ("#screenViewClose", "#screenMode", "#screenPicker"):
        expect(page.locator(part)).to_be_visible()
    opened = _geometry(page)
    assert opened["stage"] == closed["stage"], (closed["stage"], opened["stage"])
    assert opened["image"] == closed["image"], (closed["image"], opened["image"])
    bar_box = bar.bounding_box()
    tools_box = tools.bounding_box()
    assert bar_box["y"] <= 1 and tools_box["y"] >= bar_box["y"] + bar_box["height"] - 1
    # The handle stays put, clear of the bar's own controls (the title is
    # padded round it).
    assert opened["handle"] == closed["handle"], opened["handle"]
    title = page.locator("#screenViewTitle").bounding_box()
    assert title["x"] >= closed["handle"]["x"] + closed["handle"]["w"] - 1, title

    # The handle again puts them away.
    handle.click()
    expect(bar).to_be_hidden()
    expect(handle).to_have_attribute("aria-expanded", "false")

    # The mode switch is reachable through it, and the handle names the mode
    # the hidden switch holds.
    handle.click()
    _set_mode(page, "control")
    expect(handle).to_contain_text("Control")
    # Same pill either way: the bar's title is padded round one width.
    assert _geometry(page)["handle"] == closed["handle"], _geometry(page)["handle"]
    assert handle.evaluate("el => el.scrollWidth <= el.clientWidth")
    expect(page.locator("#screenKeyboard")).to_be_visible()
    expect(page.locator("#screenControl")).to_be_visible()
    expect(tools).to_be_visible(), "the mode switch leaves the bars up"
    _set_mode(page, "view")
    expect(handle).to_contain_text("View")

    # Picking a display finishes the bar's job: the reconnect, then the bars go.
    page.locator("#screenPicker .segmented-item").nth(1).click()
    wait_until(page, lambda: len(seen["urls"]) == 2, "the reconnect")
    expect(bar).to_be_hidden()
    expect(handle).to_have_attribute("aria-expanded", "false")

    # Close is reachable through the handle, and the view opens bars-away next
    # time.
    handle.click()
    page.locator("#screenViewClose").click()
    expect(page.locator("#screenView")).to_be_hidden()
    page.locator("#paneClaude > .home-head .cpu-reading").click()
    page.locator("#cpuSheetScreen").click()
    expect(page.locator("#screenView")).to_be_visible()
    expect(bar).to_be_hidden()
    expect(handle).to_have_attribute("aria-expanded", "false")


def test_a_tap_on_the_picture_puts_the_bars_away_and_is_only_that(
    authed_page: Page, base_url: str
) -> None:
    seen, page = _live_view(authed_page, base_url)
    handle = page.locator("#screenHandle")
    bar = page.locator(".screen-view-bar")

    # In Control a tap clicks the PC; with the bars up, the first one only
    # closes them.
    handle.click()
    _set_mode(page, "control")
    expect(bar).to_be_visible()
    _tap(page, 0.25, 0.75)
    expect(bar).to_be_hidden()
    expect(handle).to_have_attribute("aria-expanded", "false")
    _tap(page, 0.25, 0.75)
    wait_until(page, lambda: len(seen["inputs"]) >= 1, "the click")
    # Socket messages arrive in order: the first tap sent nothing.
    assert [m["type"] for m in seen["inputs"]] == ["click"], seen["inputs"]

    # Nor does it pan or zoom in View.
    handle.click()
    _set_mode(page, "view")
    start = _point(page, 0.5, 0.5)
    _pointer(page, "pointerdown", start)
    _pointer(page, "pointerup", start)
    expect(bar).to_be_hidden()
    expect(page.locator("#screenImage")).to_have_css("transform", "none")


def test_the_keyboard_toggle_through_the_handle_raises_the_keys_bar(
    authed_page: Page, base_url: str
) -> None:
    _, page = _live_view(authed_page, base_url)
    handle = page.locator("#screenHandle")
    keys = page.locator("#screenKeys")
    toggle = page.locator("#screenKeyboard")

    handle.click()
    _set_mode(page, "control")
    toggle.click()
    expect(keys).to_be_visible()
    expect(page.locator(".screen-view-bar")).to_be_hidden()
    geo = _geometry(page)
    keys_box = keys.bounding_box()
    assert keys_box["y"] + keys_box["height"] <= geo["vh"] + 1, keys_box
    assert geo["stage"]["h"] > 100, geo["stage"]
    # The keys on the page that is showing: the other page (#1602) is hidden
    # and has no box, so it is not a key the thumb can miss.
    sizes = page.locator(
        "#screenKeys .screen-keys-page:not([hidden]) .key-btn"
    ).evaluate_all(
        "els => els.map(e => { const r = e.getBoundingClientRect(); return [r.width, r.height]; })"
    )
    assert sizes and all(w >= 40 and h >= 44 for w, h in sizes), sizes

    # And off again the same way.
    handle.click()
    toggle.click()
    expect(keys).to_be_hidden()
    expect(toggle).to_have_attribute("aria-pressed", "false")


def test_turning_the_phone_upright_restores_the_bars(
    authed_page: Page, base_url: str
) -> None:
    _, page = _live_view(authed_page, base_url)
    handle = page.locator("#screenHandle")
    bar = page.locator(".screen-view-bar")
    tools = page.locator(".screen-view-tools")

    # Upright (even with the bars up when it turns): the bars are in the flow
    # above the picture, and there is no handle.
    handle.click()
    expect(bar).to_be_visible()
    page.set_viewport_size(_PORTRAIT)
    expect(handle).to_be_hidden()
    expect(bar).to_be_visible()
    expect(tools).to_be_visible()
    stage = page.locator("#screenStage").bounding_box()
    tools_box = tools.bounding_box()
    assert stage["y"] >= tools_box["y"] + tools_box["height"] - 1, (stage, tools_box)
    assert stage["height"] < _PORTRAIT["height"] - tools_box["height"] + 1, stage

    # Back on its side: the bars away again, not left half-open.
    page.set_viewport_size(_LANDSCAPE)
    expect(handle).to_be_visible()
    expect(bar).to_be_hidden()
    expect(handle).to_have_attribute("aria-expanded", "false")
