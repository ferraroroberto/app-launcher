"""Regression pin for #980 — the composer's image button options.

The image button in the composer's 2×2 grid is a two-option menu (the same
row-menu component as the session ⚙️ gear): **Attach image or file**
(today's inline upload flow) and **Extract text from screenshots** (the OCR
staging tray). A small dot marks the button as having options. With photo-ocr
unconfigured the OCR *option* hides (not the button): the menu would have one
row, so the button then opens the attach picker directly and the dot hides.

Each option opens a native file picker, which Playwright surfaces as a
``filechooser`` event — that is how the test proves which input each option
reaches without depending on a live photo-ocr.

The loopback harness opens every terminal as the PC mirror, where the
composer is hidden by design; it is un-hidden here to drive the real
handlers (see test_compose_bar.py).
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import OVERLAY_OPEN_MS

COMPOSER = "#terminalComposeBar"
IMAGE_BTN = f"{COMPOSER} .composer-image"
MENU = f"{COMPOSER} .composer-menu"
ATTACH_OPTION = f"{MENU} [aria-label='Attach image or file']"
OCR_OPTION = f"{MENU} [aria-label='Extract text from screenshots']"

pytestmark = pytest.mark.smoke


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


def _status_with_ocr(enabled: bool):
    def _route(route):
        resp = route.fetch()
        body = resp.json()
        body["screenshot_ocr"] = enabled
        route.fulfill(response=resp, json=body)
    return _route


def test_image_button_menu_reaches_both_options(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    authed_page.route(re.compile(r".*/api/status$"), _status_with_ocr(True))
    _open_composer(authed_page, base_url, launched_pty_session)

    image = authed_page.locator(IMAGE_BTN)
    menu = authed_page.locator(MENU)
    expect(image).to_have_class(re.compile(r"\bhas-options\b"))
    expect(menu).to_be_hidden()

    # Tap → the two-option menu opens above the composer.
    image.click()
    expect(menu).to_be_visible()
    expect(authed_page.locator(ATTACH_OPTION)).to_be_visible()
    expect(authed_page.locator(OCR_OPTION)).to_be_visible()

    # OCR option → the screenshot input (accept=image/*, multiple) opens.
    with authed_page.expect_file_chooser() as fc:
        authed_page.locator(OCR_OPTION).click()
    chooser = fc.value
    assert chooser.is_multiple()
    assert chooser.element.get_attribute("accept") == "image/*"
    expect(menu).to_be_hidden()

    # Attach option → the general attach input (no accept filter, multiple).
    image.click()
    expect(menu).to_be_visible()
    with authed_page.expect_file_chooser() as fc:
        authed_page.locator(ATTACH_OPTION).click()
    chooser = fc.value
    assert chooser.is_multiple()
    assert not chooser.element.get_attribute("accept")
    expect(menu).to_be_hidden()


def test_image_button_opens_picker_directly_without_ocr(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    authed_page.route(re.compile(r".*/api/status$"), _status_with_ocr(False))
    _open_composer(authed_page, base_url, launched_pty_session)

    image = authed_page.locator(IMAGE_BTN)
    expect(image).not_to_have_class(re.compile(r"\bhas-options\b"))
    expect(authed_page.locator(OCR_OPTION)).to_have_attribute("hidden", "")

    # One option left → no menu; the tap goes straight to the attach picker.
    with authed_page.expect_file_chooser() as fc:
        image.click()
    assert not fc.value.element.get_attribute("accept")
    expect(authed_page.locator(MENU)).to_be_hidden()
