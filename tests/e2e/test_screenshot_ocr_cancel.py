"""Composer OCR extraction (#1420): a long run stays cancellable, and a partly
read take says so.

photo-ocr can legitimately hold ``/api/ocr`` for up to ~15 minutes on a slow
hub (photo-ocr#166), so the **Extract text** button — which used to go
``disabled`` for the whole wait — now follows #1413's busy/cancel contract: it
is ``aria-busy`` (never ``disabled``: iOS fires no tap on a disabled button),
reads "Cancel extraction", and a second tap aborts the request, keeps the
staged screenshots and leaves the button ready for another try.

``/api/ocr`` is route-mocked and held until the test answers it, so the busy
state is on screen for as long as an assertion needs. Nothing reaches a live
photo-ocr or LLM hub.
"""

from __future__ import annotations

import base64
import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import OVERLAY_OPEN_MS, HeldUploads, wait_until

_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk"
    "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

COMPOSER = "#terminalComposeBar"
EXTRACT = f"{COMPOSER} .ocr-extract"
TEXTAREA = f"{COMPOSER} .composer-input"
THUMBS = f"{COMPOSER} .ocr-thumbs .ocr-thumb"
TOAST = "#toast"

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]


def _open_compose(page: Page, base_url: str, sid: str) -> None:
    page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")
    page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)
    page.wait_for_function(
        "() => document.getElementById('terminalStatus') "
        "&& document.getElementById('terminalStatus').hidden === true",
        timeout=OVERLAY_OPEN_MS,
    )
    page.evaluate("document.getElementById('terminalComposeBar').hidden = false")
    expect(page.locator(COMPOSER)).to_be_visible()


def _stage(page: Page, *names: str) -> None:
    page.locator(f"{COMPOSER} .composer-ocr-input").set_input_files(
        files=[{"name": n, "mimeType": "image/png", "buffer": _PNG_1x1} for n in names]
    )


def test_second_tap_cancels_a_long_extraction(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    page = authed_page
    held = HeldUploads(page, re.compile(r".*/api/ocr$"))
    failed: list = []
    page.on("requestfailed", lambda r: failed.append(r.url) if r.url.endswith("/api/ocr") else None)
    _open_compose(page, base_url, launched_pty_session)
    _stage(page, "e2e-stub-a.png", "e2e-stub-b.png")

    extract = page.locator(EXTRACT)
    extract.click()
    held.wait_for(1)

    # Busy, but still tappable — a disabled button would swallow the cancel tap.
    expect(extract).to_have_attribute("aria-busy", "true")
    expect(extract).not_to_be_disabled()
    expect(extract).to_have_attribute("aria-label", "Cancel extraction")

    extract.click()

    expect(page.locator(TOAST)).to_contain_text("Extraction cancelled")
    # The request was aborted client-side, not left dangling.
    wait_until(page, lambda: bool(failed), "the /api/ocr request to be aborted")
    # Ready again, screenshots still staged for a retry, nothing inserted.
    expect(extract).not_to_have_attribute("aria-busy", "true")
    expect(extract).to_have_text("Extract text (2)")
    expect(page.locator(THUMBS)).to_have_count(2)
    expect(page.locator(TEXTAREA)).to_have_value("")


def test_partial_take_inserts_text_and_says_how_many_were_missed(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    page = authed_page
    page.route(
        re.compile(r".*/api/ocr$"),
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "text": "page one\n[missing: photo 2 (e2e-stub-b.png) could not be read]",
                "missing_photos": ["e2e-stub-b.png"],
            }),
        ),
    )
    _open_compose(page, base_url, launched_pty_session)
    _stage(page, "e2e-stub-a.png", "e2e-stub-b.png")

    page.locator(EXTRACT).click()

    # The text lands (inline marker included), and the toast is not the plain
    # "Text extracted" success: it names how many screenshots could not be read.
    expect(page.locator(TEXTAREA)).to_have_value(re.compile(r"page one\n\[missing: photo 2"))
    expect(page.locator(TOAST)).to_contain_text("1 of 2 screenshots could not be read")
    expect(page.locator(THUMBS)).to_have_count(0)
