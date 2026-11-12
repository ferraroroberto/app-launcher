"""The Board's project filter is a type-to-filter combobox with a clear button.

Typing narrows the options by name, Enter or a tap commits one, Escape drops the
typed text, and the x shows only while the field holds text. The committed value
stays in the hidden `#boardDispatchRepo`, so the lanes filter as before.
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.board._support import _mock_board, _open_board

pytestmark = pytest.mark.smoke

_PROJECTS = ("app-launcher", "voice-transcriber", "life-os", "photo-ocr")

_INPUT = "#boardDispatchRepoInput"
_LIST = "#boardDispatchRepoList"
_CLEAR = "#boardDispatchRepoClear"
_HIDDEN = "#boardDispatchRepo"


def _open(page: Page, base_url: str) -> None:
    page.route(
        re.compile(r".*/api/apps$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"scan_root": "", "apps": [
                {"id": f"cc-{name}", "kind": "claude-code", "name": name,
                 "project_dir": f"E:/automation/{name}"}
                for name in _PROJECTS
            ]}),
        ),
    )
    _mock_board(page)
    _open_board(page, base_url)
    # The poll that lands /api/apps fills the list; wait for it before typing.
    page.locator(_INPUT).click()
    expect(page.locator(f"{_LIST} li[data-repo]")).to_have_count(
        len(_PROJECTS) + 1, timeout=15_000)
    page.keyboard.press("Escape")
    expect(page.locator(_LIST)).to_be_hidden()


def _options(page: Page):
    return page.locator(f"{_LIST} li[data-repo]")


def test_typing_narrows_commits_and_clears(authed_page: Page, base_url: str) -> None:
    page = authed_page
    _open(page, base_url)
    field = page.locator(_INPUT)
    clear = page.locator(_CLEAR)

    assert field.evaluate("el => el.tagName") == "INPUT"
    expect(field).to_have_attribute("role", "combobox")
    expect(field).to_have_value("")
    expect(field).to_have_attribute("placeholder", "All projects")
    expect(clear).to_be_hidden()

    field.click()
    expect(_options(page)).to_have_count(5)  # "All projects" + 4
    page.keyboard.type("PHO")
    expect(page.locator(_LIST)).to_be_visible()
    # Case-insensitive substring; "All projects" stays first.
    expect(_options(page)).to_have_text(["All projects", "photo-ocr"])
    expect(clear).to_be_visible()
    # Typing alone commits nothing.
    expect(page.locator(_HIDDEN)).to_have_value("")
    expect(page.locator("#boardColBacklog .board-count")).to_have_text("1")

    page.keyboard.press("Enter")
    expect(page.locator(_LIST)).to_be_hidden()
    expect(page.locator(_HIDDEN)).to_have_value("photo-ocr")
    expect(field).to_have_value("photo-ocr")
    expect(page.locator("#boardColBacklog .board-count")).to_have_text("0")
    expect(page.locator("#boardColYours .board-count")).to_have_text("1")
    expect(clear).to_be_visible()

    clear.click()
    expect(field).to_have_value("")
    expect(page.locator(_HIDDEN)).to_have_value("")
    expect(clear).to_be_hidden()
    expect(page.locator("#boardColBacklog .board-count")).to_have_text("1")


def test_clear_drops_typed_text_without_a_selection(authed_page: Page, base_url: str) -> None:
    page = authed_page
    _open(page, base_url)
    field = page.locator(_INPUT)
    field.click()
    page.keyboard.type("voi")
    expect(_options(page)).to_have_count(2)
    page.locator(_CLEAR).click()
    expect(field).to_have_value("")
    expect(page.locator(_CLEAR)).to_be_hidden()
    expect(page.locator(_HIDDEN)).to_have_value("")
    # The field keeps focus and the full list is back for the next word.
    expect(field).to_be_focused()
    expect(_options(page)).to_have_count(5)


def test_arrow_keys_enter_and_escape(authed_page: Page, base_url: str) -> None:
    page = authed_page
    _open(page, base_url)
    field = page.locator(_INPUT)

    field.click()
    page.keyboard.press("ArrowDown")  # All projects
    page.keyboard.press("ArrowDown")  # app-launcher
    expect(_options(page).nth(1)).to_have_attribute("data-active", "true")
    page.keyboard.press("Enter")
    expect(page.locator(_HIDDEN)).to_have_value("app-launcher")
    expect(field).to_have_value("app-launcher")

    # Typing picks the first match, so Enter alone takes it.
    field.click()
    page.keyboard.press("Control+a")
    page.keyboard.type("vo")
    page.keyboard.press("Enter")
    expect(page.locator(_HIDDEN)).to_have_value("voice-transcriber")

    # Escape closes the list and drops what was typed, keeping the committed pick.
    field.click()
    page.keyboard.press("Control+a")
    page.keyboard.type("zzz")
    expect(_options(page)).to_have_text(["All projects"])
    expect(page.locator(f"{_LIST} .board-repo-empty")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator(_LIST)).to_be_hidden()
    expect(field).to_have_value("voice-transcriber")
    expect(page.locator(_HIDDEN)).to_have_value("voice-transcriber")


@pytest.mark.iphone
def test_clear_button_is_a_real_touch_target(authed_page: Page, base_url: str) -> None:
    page = authed_page
    _open(page, base_url)
    page.locator(_INPUT).click()
    page.keyboard.type("life")
    box = page.locator(_CLEAR).bounding_box()
    assert box and box["width"] >= 44 - 0.5 and box["height"] >= 44 - 0.5, box
    outer = page.locator(".board-repo-combo").bounding_box()
    assert outer and box["x"] + box["width"] <= outer["x"] + outer["width"] + 0.5, (box, outer)
