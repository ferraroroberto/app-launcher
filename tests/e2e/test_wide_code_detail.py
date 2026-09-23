"""Regression pin for #1135: the Code tab's list-and-detail at 1100px and up.

design.md Layout, "Master-detail" (fleet-config#968): on a wide window the
session view opened from the Code tab used to cover the list it came from.
Now it docks into a detail pane beside the list (list : detail = 1 : 1.5 of
the space right of the rail), the row it shows keeps the accent tint, and
another row opens in place of it rather than closing it. With nothing open
the pane says so. Leaving the Code tab closes the view. Below 1100px the view
still covers the whole window.

Detached rows are used because a desktop tap on a full-control row opens its
own PC window instead (#282, test_desktop_session_mirror.py); a detached
row's Chat is the in-page view on every device. Data is synthetic and every
fetch the rows depend on is mocked (#510).
"""
from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import stable_read

pytestmark = pytest.mark.smoke

RAIL = 80
_A = "sid-wide-a-1135"
_B = "sid-wide-b-1135"


def _row_data(sid: str, title: str) -> dict:
    return {
        "session_id": sid, "kind": "remote", "agent": "claude",
        "project_dir": "E:/automation/wideproj", "name": "wideproj",
        "alive": True, "started_at": 1_790_150_400.0,
        "live_title": "", "prompt_title": "", "manual_title": title,
    }


def _mock(page: Page) -> None:
    def fulfill(body):
        return lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(body)
        )

    page.route(re.compile(r".*/api/claude-code/git-status$"), fulfill({"projects": []}))
    page.route(
        re.compile(r".*/api/claude-code/sessions$"),
        fulfill({"sessions": [_row_data(_A, "Wide demo A"), _row_data(_B, "Wide demo B")]}),
    )
    page.route(
        re.compile(r".*/api/claude-code/sessions/[^/]+/transcript(\?.*)?$"),
        fulfill({
            "available": True, "source": "native", "reason": None,
            "entries": [], "next_cursor": None, "session_id": _A,
        }),
    )


def _row(page: Page, sid: str):
    return page.locator(f'#sessionsList li.session-item[data-session-id="{sid}"]')


def _box(page: Page, selector: str) -> dict:
    box = stable_read(lambda: page.locator(selector).bounding_box())
    assert box, f"{selector} has no layout box"
    return box


def _fixed_width(page: Page) -> float:
    """The width a `position: fixed; inset: 0` layer gets — the viewport less
    the page's reserved scrollbar gutter, which `clientWidth` does not report."""
    return page.evaluate(
        """() => {
          const probe = document.createElement('div');
          probe.style.cssText = 'position:fixed;left:0;right:0;top:0;height:1px';
          document.body.appendChild(probe);
          const width = probe.getBoundingClientRect().width;
          probe.remove();
          return width;
        }"""
    )


def _skip_unless_desktop(browser_name: str) -> None:
    if browser_name != "chromium":
        pytest.skip("fine-pointer layout; the WebKit projection is an iPhone")


def test_session_opens_beside_the_list_and_switching_keeps_it_open(
    authed_page: Page, base_url: str, browser_name: str
) -> None:
    _skip_unless_desktop(browser_name)
    page = authed_page
    page.set_viewport_size({"width": 1440, "height": 900})
    _mock(page)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    overlay = page.locator("#terminalOverlay")
    empty = page.locator("#sessionDetailEmpty")

    # Nothing open: the detail pane says so, beside the list.
    expect(_row(page, _A)).to_be_visible()
    expect(empty).to_be_visible()
    width = _fixed_width(page)
    list_box = _box(page, ".app")
    detail_left = RAIL + (width - RAIL) / 2.5
    assert abs(list_box["x"] - RAIL) <= 1, f"list pane starts at {list_box['x']}"
    assert abs(list_box["x"] + list_box["width"] - detail_left) <= 1, list_box
    assert abs(_box(page, "#sessionDetailEmpty")["x"] - detail_left) <= 1

    # Open A: the view docks into the detail pane and the list stays usable.
    _row(page, _A).locator(".session-open").click()
    expect(overlay).to_be_visible()
    expect(empty).to_be_hidden()
    view = _box(page, "#terminalOverlay")
    assert abs(view["x"] - detail_left) <= 1, f"view starts at {view['x']}, not {detail_left}"
    assert abs(view["x"] + view["width"] - width) <= 1, "view must reach the window's edge"
    expect(page.locator("nav.tabs")).to_be_visible()
    expect(_row(page, _A)).to_have_attribute("aria-current", "true")
    expect(page.locator("#terminalTitle")).to_contain_text("Wide demo A")

    # Switching sessions replaces the view in place; it never closes.
    _row(page, _B).locator(".session-open").click()
    expect(overlay).to_be_visible()
    expect(page.locator("#terminalTitle")).to_contain_text("Wide demo B")
    expect(_row(page, _B)).to_have_attribute("aria-current", "true")
    expect(_row(page, _A)).not_to_have_attribute("aria-current", "true")

    # Leaving the Code tab closes the view; coming back shows the empty pane.
    page.locator("#tabBoard").click()
    expect(overlay).to_be_hidden()
    expect(empty).to_be_hidden()
    page.locator("#tabClaude").click()
    expect(empty).to_be_visible()
    expect(_row(page, _B)).not_to_have_attribute("aria-current", "true")


def test_below_1100px_the_view_still_covers_the_window(
    authed_page: Page, base_url: str, browser_name: str
) -> None:
    _skip_unless_desktop(browser_name)
    page = authed_page
    page.set_viewport_size({"width": 1099, "height": 800})
    _mock(page)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    expect(page.locator("#sessionDetailEmpty")).to_be_hidden()
    _row(page, _A).locator(".session-open").click()
    expect(page.locator("#terminalOverlay")).to_be_visible()
    view = _box(page, "#terminalOverlay")
    width = _fixed_width(page)
    assert view["x"] == 0 and view["width"] == width, f"view no longer covers: {view}"
