"""The Board project filter's type-to-filter checks, run from test_board_backlog.

Typing narrows the options by name, Enter or a tap commits one, Escape drops the
typed text, and the x shows only while the field holds text. The committed value
stays in the hidden `#boardDispatchRepo`, so the lanes filter as before. These
ride the existing project-filter test (same mocked apps and board) as one node.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

_INPUT = "#boardDispatchRepoInput"
_LIST = "#boardDispatchRepoList"
_CLEAR = "#boardDispatchRepoClear"
_HIDDEN = "#boardDispatchRepo"


def _options(page: Page):
    return page.locator(f"{_LIST} li[data-repo]")


def _narrow_commit_and_clear(page: Page) -> None:
    field = page.locator(_INPUT)
    clear = page.locator(_CLEAR)

    assert field.evaluate("el => el.tagName") == "INPUT"
    expect(field).to_have_attribute("role", "combobox")
    expect(field).to_have_value("")
    expect(clear).to_be_hidden()

    field.click()
    expect(_options(page)).to_have_count(5)  # "All projects" + 4
    page.keyboard.type("PHO")
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

    # A 44px target that stays inside the field.
    box, outer = clear.bounding_box(), page.locator(".board-repo-combo").bounding_box()
    assert box and box["width"] >= 44 - 0.5 and box["height"] >= 44 - 0.5, box
    assert outer and box["x"] + box["width"] <= outer["x"] + outer["width"] + 0.5, (box, outer)

    clear.click()
    expect(field).to_have_value("")
    expect(page.locator(_HIDDEN)).to_have_value("")
    expect(page.locator("#boardColBacklog .board-count")).to_have_text("1")
    # The field keeps focus and the full list is back for the next word.
    expect(field).to_be_focused()
    expect(_options(page)).to_have_count(5)
    page.keyboard.press("Escape")


def _clear_drops_typed_text(page: Page) -> None:
    field = page.locator(_INPUT)
    field.click()
    page.keyboard.type("voi")
    expect(_options(page)).to_have_count(2)
    page.locator(_CLEAR).click()
    expect(field).to_have_value("")
    expect(page.locator(_CLEAR)).to_be_hidden()
    expect(page.locator(_HIDDEN)).to_have_value("")
    page.keyboard.press("Escape")


def _arrow_keys_enter_and_escape(page: Page) -> None:
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

    # Back to All projects so the caller's later steps see the whole board.
    page.locator(_CLEAR).click()
    expect(page.locator(_HIDDEN)).to_have_value("")
    page.keyboard.press("Escape")


def check_repo_typeahead(page: Page) -> None:
    """Run every type-to-filter check, leaving the filter on All projects."""
    _narrow_commit_and_clear(page)
    _clear_drops_typed_text(page)
    _arrow_keys_enter_and_escape(page)
