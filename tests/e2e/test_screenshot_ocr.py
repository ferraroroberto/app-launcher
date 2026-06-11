"""Regression pin for issue #171 — compose-bar screenshot-OCR staging.

The feature: the 📷 button **stages** screenshots into a tray above the
compose bar (accumulating across taps), and a single **Extract text (N)**
button later sends them all to photo-ocr in one call so it can collate +
deduplicate. This test pins the *staging* client logic — accumulate, count,
remove — which needs no live photo-ocr (the extraction call itself does and
is covered by the unit/integration tests for /api/ocr).

Uses the same mirror un-hide trick as test_compose_bar.py: the loopback e2e
harness opens every terminal as the PC mirror, where the ✏️ button is hidden
by design, so we un-hide it to drive the real compose handlers.
"""

from __future__ import annotations

import base64

import pytest
from playwright.sync_api import Page, expect

# 1x1 transparent PNG — smallest valid image; staging only needs a File.
_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk"
    "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

pytestmark = pytest.mark.smoke


def _open_compose(page: Page, base_url: str, sid: str) -> None:
    page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")
    page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=10_000)
    page.wait_for_function(
        "() => document.getElementById('terminalStatus') "
        "&& document.getElementById('terminalStatus').hidden === true",
        timeout=10_000,
    )
    # Un-hide + open the compose bar (mirror trick — see module docstring).
    page.evaluate("document.getElementById('terminalCompose').hidden = false")
    page.locator("#terminalCompose").click()
    expect(page.locator("#terminalComposeBar")).to_be_visible()


def _stage(page: Page, name: str) -> None:
    page.locator("#terminalScreenshotInput").set_input_files(
        files=[{"name": name, "mimeType": "image/png", "buffer": _PNG_1x1}]
    )


def test_screenshot_staging_accumulates_and_removes(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    page = authed_page
    _open_compose(page, base_url, launched_pty_session)

    tray = page.locator("#terminalOcrTray")
    extract = page.locator("#terminalOcrExtract")
    thumbs = page.locator("#terminalOcrThumbs .ocr-thumb")

    # Nothing staged yet → tray hidden.
    expect(tray).to_be_hidden()

    # Stage one → tray shows one thumbnail, Extract reads (1).
    _stage(page, "a.png")
    expect(tray).to_be_visible()
    expect(thumbs).to_have_count(1)
    expect(extract).to_have_text("📷 Extract text (1)")

    # Stage another (separate tap) → accumulates to 2, not sent per-image.
    _stage(page, "b.png")
    expect(thumbs).to_have_count(2)
    expect(extract).to_have_text("📷 Extract text (2)")

    # Remove one → back to 1.
    page.locator("#terminalOcrThumbs .ocr-thumb-x").first.click()
    expect(thumbs).to_have_count(1)
    expect(extract).to_have_text("📷 Extract text (1)")


def test_screenshot_staging_cleared_when_compose_closes(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    page = authed_page
    _open_compose(page, base_url, launched_pty_session)

    _stage(page, "a.png")
    expect(page.locator("#terminalOcrTray")).to_be_visible()

    # Closing the compose bar drops staged images (no leak across sessions).
    page.locator("#terminalCompose").click()
    expect(page.locator("#terminalComposeBar")).to_be_hidden()
    expect(page.locator("#terminalOcrTray")).to_be_hidden()
    expect(page.locator("#terminalOcrThumbs .ocr-thumb")).to_have_count(0)
