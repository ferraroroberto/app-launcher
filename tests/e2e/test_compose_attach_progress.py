"""Composer attach progress (#1354): a multi-file pick says what it is doing.

The image button swaps to an hourglass over "N/M" and a status line above the
textarea reads "Uploading N of M · name" while the queue runs; one summary
toast ends the run and names every failure; a file over the host's 12 MB
limit never leaves the browser; a second pick made mid-batch queues behind
the first, in pick order, with nothing sent twice.

The upload endpoint is route-mocked and each request is held until the test
answers it (``HeldUploads``), so every state between two uploads is on screen
for as long as an assertion needs. Every file is named ``e2e-stub-…`` per
the marker in tests/e2e/conftest.py's leak check (#922), though nothing here
reaches the host. The composer under test is the terminal's — the same
``composer.js`` Chat and the Board drawer mount; the answer sheet's field
mount (which has no Send) has its own test in test_board_chief.py.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import HeldUploads
from tests.e2e.test_compose_bar import (
    ATTACH_INPUT, COMPOSER, INPUT, _PNG_1x1, _open_terminal, _show_composer,
)

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_UPLOAD_ROUTE = re.compile(r".*/api/claude-code/sessions/[^/]+/image(?:\?.*)?$")
STATUS = f"{COMPOSER} .composer-upload-status"
IMAGE_BTN = f"{COMPOSER} .composer-image"


def _png(name: str) -> dict:
    return {"name": name, "mimeType": "image/png", "buffer": _PNG_1x1}


def _path(name: str) -> str:
    return f"E:/tmp/.launcher-tmp/{name}"


def _ready(page: Page, base_url: str, sid: str) -> HeldUploads:
    held = HeldUploads(page, _UPLOAD_ROUTE)
    _open_terminal(page, base_url, sid)
    _show_composer(page)
    return held


def test_progress_shows_on_the_button_and_status_line_then_clears(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    held = _ready(authed_page, base_url, launched_pty_session)
    authed_page.locator(ATTACH_INPUT).set_input_files(
        files=[_png("e2e-stub-a.png"), _png("e2e-stub-b.png"), _png("e2e-stub-c.png")]
    )
    held.wait_for(1)
    status = authed_page.locator(STATUS)
    button = authed_page.locator(IMAGE_BTN)
    expect(status).to_have_text("Uploading 1 of 3 · e2e-stub-a.png")
    expect(button).to_have_class(re.compile(r"\bis-uploading\b"))
    expect(button).to_have_attribute("aria-busy", "true")
    expect(button.locator(".composer-upload-count")).to_have_text("1/3")
    expect(button.locator('use[href="#i-hourglass"]')).to_have_count(1)
    # The button is still tappable: a second pick queues, it is not refused.
    expect(button).to_be_enabled()
    # Progress must not steal focus: the keyboard stays up (#450).
    assert authed_page.evaluate(
        "() => document.activeElement && document.activeElement.classList.contains('composer-input')"
    )

    held.ok(0, _path("e2e-stub-a.png"))
    held.wait_for(2)
    expect(status).to_have_text("Uploading 2 of 3 · e2e-stub-b.png")
    expect(button.locator(".composer-upload-count")).to_have_text("2/3")
    held.ok(1, _path("e2e-stub-b.png"))
    held.wait_for(3)
    held.ok(2, _path("e2e-stub-c.png"))

    expect(status).to_be_hidden()
    expect(button).not_to_have_class(re.compile(r"\bis-uploading\b"))
    expect(button.locator('use[href="#i-image"]')).to_have_count(1)
    expect(authed_page.locator(INPUT)).to_have_value(
        "\n\n".join(_path(n) for n in ("e2e-stub-a.png", "e2e-stub-b.png", "e2e-stub-c.png"))
    )
    expect(authed_page.locator("#toast")).to_contain_text(
        "Uploaded 3 files — paths added to the message."
    )


def test_a_failure_mid_batch_is_named_in_the_summary_and_the_rest_land(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    held = _ready(authed_page, base_url, launched_pty_session)
    authed_page.locator(ATTACH_INPUT).set_input_files(
        files=[_png("e2e-stub-a.png"), _png("e2e-stub-b.png"), _png("e2e-stub-c.png")]
    )
    held.wait_for(1)
    held.ok(0, _path("e2e-stub-a.png"))
    held.wait_for(2)
    held.fail(1, "disk full")
    held.wait_for(3)
    held.ok(2, _path("e2e-stub-c.png"))

    toast = authed_page.locator("#toast")
    expect(toast).to_have_text("Uploaded 2 of 3 — 1 failed: e2e-stub-b.png (disk full)")
    expect(toast).to_have_class(re.compile(r"\berror\b"))
    expect(authed_page.locator(INPUT)).to_have_value(
        _path("e2e-stub-a.png") + "\n\n" + _path("e2e-stub-c.png")
    )
    expect(authed_page.locator(STATUS)).to_be_hidden()


def test_an_oversize_file_is_refused_before_any_upload_and_names_the_limit(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    held = _ready(authed_page, base_url, launched_pty_session)
    big = {"name": "e2e-stub-big.png", "mimeType": "image/png",
           "buffer": b"\0" * (13 * 1024 * 1024)}
    authed_page.locator(ATTACH_INPUT).set_input_files(files=[big, _png("e2e-stub-a.png")])
    # Only the small file is ever requested: the big one costs no bytes.
    held.wait_for(1)
    held.ok(0, _path("e2e-stub-a.png"))

    expect(authed_page.locator("#toast")).to_have_text(
        "Uploaded 1 of 2 — 1 failed: e2e-stub-big.png (13 MB, limit 12 MB)"
    )
    expect(authed_page.locator(INPUT)).to_have_value(_path("e2e-stub-a.png"))
    assert held.count == 1, "the oversize file must never be requested"


def test_a_second_pick_mid_batch_queues_in_order_without_duplicates(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    held = _ready(authed_page, base_url, launched_pty_session)
    authed_page.locator(ATTACH_INPUT).set_input_files(
        files=[_png("e2e-stub-a.png"), _png("e2e-stub-b.png")]
    )
    held.wait_for(1)
    expect(authed_page.locator(STATUS)).to_have_text("Uploading 1 of 2 · e2e-stub-a.png")
    # The second pick lands while the first file is still uploading.
    authed_page.locator(ATTACH_INPUT).set_input_files(files=[_png("e2e-stub-c.png")])
    expect(authed_page.locator(STATUS)).to_have_text("Uploading 1 of 3 · e2e-stub-a.png")
    assert held.count == 1, "a queued pick must not start beside the running upload"

    held.ok(0, _path("e2e-stub-a.png"))
    held.wait_for(2)
    held.ok(1, _path("e2e-stub-b.png"))
    held.wait_for(3)
    expect(authed_page.locator(STATUS)).to_have_text("Uploading 3 of 3 · e2e-stub-c.png")
    held.ok(2, _path("e2e-stub-c.png"))

    expect(authed_page.locator(STATUS)).to_be_hidden()
    expect(authed_page.locator(INPUT)).to_have_value(
        "\n\n".join(_path(n) for n in ("e2e-stub-a.png", "e2e-stub-b.png", "e2e-stub-c.png"))
    )
    expect(authed_page.locator("#toast")).to_contain_text("Uploaded 3 files")
    assert held.count == 3, "each file is uploaded exactly once"
