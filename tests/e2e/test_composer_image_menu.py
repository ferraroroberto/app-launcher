"""Regression pin for #980 / #1430 — the composer's 📎 attach menu.

The attach button in the composer's 2×2 grid is a paperclip (#1430; it was a
photo glyph) that opens a menu — the same row-menu component as the session
⚙️ gear — of everything that puts something into the message: **Photo**,
**Extract text (OCR)**, **Attach file (to context)** and **Attach large file
(not read)**. With photo-ocr unconfigured only the OCR row hides; the other
three always show, so the button always opens the menu. The button never
carries a "has options" dot (#1418): it read as a stray artefact once the
composer buttons became plain glyph icons.

Each option opens a native file picker, which Playwright surfaces as a
``filechooser`` event — that is how the test proves which input each option
reaches without depending on a live photo-ocr.

The large-file row (#1430) uploads the raw file to ``/large-file`` and appends
a line that names the path and tells the agent not to read it, round-tripped
here through the disposable session-host; the streaming save, the limit and
the cleanup are pinned server-side in ``tests/test_large_upload.py``.

The loopback harness opens every terminal as the PC mirror, where the
composer is hidden by design; it is un-hidden here to drive the real
handlers (see test_compose_bar.py).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import OVERLAY_OPEN_MS

COMPOSER = "#terminalComposeBar"
IMAGE_BTN = f"{COMPOSER} .composer-image"
MENU = f"{COMPOSER} .composer-menu"
PHOTO_OPTION = f"{MENU} [aria-label='Photo']"
OCR_OPTION = f"{MENU} [aria-label='Extract text (OCR)']"
ATTACH_OPTION = f"{MENU} [aria-label='Attach file (to context)']"
LARGE_OPTION = f"{MENU} [aria-label='Attach large file (not read)']"

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]


def _open_composer(page: Page, base_url: str, sid: str) -> None:
    page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")
    page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)
    page.wait_for_function(
        "() => document.getElementById('terminalStatus') "
        "&& document.getElementById('terminalStatus').hidden === true",
        timeout=OVERLAY_OPEN_MS,
    )
    page.evaluate("document.getElementById('terminalComposeBar').hidden = false")
    expect(page.locator(COMPOSER)).to_be_visible()


def _expect_no_dot(image) -> None:
    """The attach button draws nothing in ::before/::after beyond the vendored
    icon-button's own invisible hit-target expansion (#1418)."""
    after = image.evaluate("el => getComputedStyle(el, '::after').content")
    assert after in ("none", "normal"), f"attach button grew an ::after: {after!r}"


def _status_with_ocr(enabled: bool):
    def _route(route):
        resp = route.fetch()
        body = resp.json()
        body["screenshot_ocr"] = enabled
        route.fulfill(response=resp, json=body)
    return _route


def _pick(page: Page, option: str):
    """Open the menu, tap ``option``, return the file chooser it raised."""
    page.locator(IMAGE_BTN).click()
    expect(page.locator(MENU)).to_be_visible()
    with page.expect_file_chooser() as fc:
        page.locator(option).click()
    expect(page.locator(MENU)).to_be_hidden()
    return fc.value


def test_attach_button_menu_reaches_all_four_options(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    authed_page.route(re.compile(r".*/api/status$"), _status_with_ocr(True))
    _open_composer(authed_page, base_url, launched_pty_session)

    image = authed_page.locator(IMAGE_BTN)
    expect(image.locator('use[href="#i-paperclip"]')).to_have_count(1)
    expect(image).to_have_class(re.compile(r"\bhas-options\b"))
    _expect_no_dot(image)
    expect(authed_page.locator(MENU)).to_be_hidden()

    image.click()
    for option in (PHOTO_OPTION, OCR_OPTION, ATTACH_OPTION, LARGE_OPTION):
        expect(authed_page.locator(option)).to_be_visible()
    image.click()  # the anchor toggles its menu shut
    expect(authed_page.locator(MENU)).to_be_hidden()

    # Photo → an image-only, multi-pick input.
    chooser = _pick(authed_page, PHOTO_OPTION)
    assert chooser.is_multiple()
    assert chooser.element.get_attribute("accept") == "image/*"
    assert "composer-photo-input" in chooser.element.get_attribute("class")

    # OCR → the screenshot staging input (accept=image/*, multiple).
    chooser = _pick(authed_page, OCR_OPTION)
    assert chooser.element.get_attribute("accept") == "image/*"
    assert "composer-ocr-input" in chooser.element.get_attribute("class")

    # Attach file → the general attach input (no accept filter, multiple).
    chooser = _pick(authed_page, ATTACH_OPTION)
    assert chooser.is_multiple()
    assert not chooser.element.get_attribute("accept")
    assert "composer-attach-input" in chooser.element.get_attribute("class")

    # Large file → its own input (no accept filter).
    chooser = _pick(authed_page, LARGE_OPTION)
    assert not chooser.element.get_attribute("accept")
    assert "composer-large-input" in chooser.element.get_attribute("class")


def test_attach_menu_without_ocr_keeps_three_options(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    authed_page.route(re.compile(r".*/api/status$"), _status_with_ocr(False))
    _open_composer(authed_page, base_url, launched_pty_session)

    image = authed_page.locator(IMAGE_BTN)
    _expect_no_dot(image)
    expect(authed_page.locator(OCR_OPTION)).to_have_attribute("hidden", "")

    image.click()
    expect(authed_page.locator(MENU)).to_be_visible()
    for option in (PHOTO_OPTION, ATTACH_OPTION, LARGE_OPTION):
        expect(authed_page.locator(option)).to_be_visible()
    expect(authed_page.locator(OCR_OPTION)).to_be_hidden()


def test_large_file_round_trip_appends_a_not_read_line(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """#1430, end to end: the large-file input uploads the raw file through the
    webapp to the disposable session-host, which stores it under its large leaf,
    and the composer appends a line telling the agent not to read it, with the
    stored path alone on its last line.

    Not mocked: WebKit does not expose a File request body to Playwright's
    route interception (it reads as 0 bytes), so the stored file on disk is
    the proof of what was sent."""
    _open_composer(authed_page, base_url, launched_pty_session)

    payload = b"z" * (3 * 1024 * 1024)
    authed_page.locator(f"{COMPOSER} .composer-large-input").set_input_files(
        files=[{"name": "e2e-stub-scans.zip", "mimeType": "application/zip", "buffer": payload}]
    )

    value = authed_page.locator(f"{COMPOSER} .composer-input")
    expect(value).to_have_value(re.compile(r"^Large file \(3 MB\)"))
    text = value.input_value()
    assert "Do not read it into context" in text
    stored = Path(text.splitlines()[-1])
    assert stored.parent.parent.name == "large", stored
    assert stored.name.endswith("-e2e-stub-scans.zip"), stored
    assert stored.stat().st_size == len(payload)
