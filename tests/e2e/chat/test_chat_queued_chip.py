"""Chat's queued chip: messages the agent has not taken yet, listed in a
sheet, discarded where the session-host can, and a second send of the same
text asked about first."""

from __future__ import annotations

import json as _json
import re
from datetime import datetime, timezone

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.waits import OVERLAY_OPEN_MS

from tests.e2e.chat._support import _SID, _boot, _open_chat, _tick, _turn


pytestmark = pytest.mark.smoke

# Sent half a minute before the stub's turns are stamped (10:01:00Z), so a
# user turn carrying the same text reads as the agent having taken it.
_SENT_AT = datetime(2026, 9, 19, 10, 0, 30, tzinfo=timezone.utc).timestamp()
_QUEUED = {
    "id": "q1", "text": "also check the ferry times", "truncated": False,
    "at": _SENT_AT, "state": "queued", "confirmed": True, "removable": True,
}
_IN_BOX = {
    "id": "c1", "text": "then open the PR\nbut do not merge", "truncated": False,
    "at": _SENT_AT, "state": "in_composer", "confirmed": True, "removable": False,
}


@pytest.mark.iphone
def test_the_chip_lists_pending_messages_and_discards_one(authed_page: Page, base_url: str) -> None:
    page = authed_page
    tr = _boot(page, base_url)
    tr.pending_input = [_QUEUED, _IN_BOX]
    discards: list = []

    def _discard(route) -> None:
        discards.append(route.request.method)
        tr.pending_input = [_IN_BOX]
        route.fulfill(status=200, content_type="application/json", body=_json.dumps({
            "ok": True, "reason": "discarded", "detail": "Removed from the agent's queue", "ids": ["q1"],
        }))

    page.route(re.compile(r".*/api/claude-code/sessions/" + _SID + r"/pending-input/q1$"), _discard)
    _open_chat(page)

    chip = page.locator("#chatQueuedChip")
    expect(chip).to_have_text("2 queued", timeout=OVERLAY_OPEN_MS)
    box = chip.bounding_box()
    assert box is not None and box["height"] >= 44

    chip.click()
    sheet = page.locator("#pendingInputDialog")
    expect(sheet).to_be_visible()
    rows = sheet.locator(".pending-row")
    expect(rows).to_have_count(2)
    expect(rows.nth(0)).to_contain_text("Queued in the agent")
    expect(rows.nth(0).locator(".pending-text")).to_have_text("also check the ferry times")
    # The text as typed, line breaks kept, and honest about what can't be removed.
    expect(rows.nth(1).locator(".pending-text")).to_have_text("then open the PR\nbut do not merge")
    expect(rows.nth(1).locator(".pending-fixed")).to_have_text("Can’t be removed from here")
    expect(rows.nth(1).locator(".pending-discard")).to_have_count(0)

    rows.nth(0).locator(".pending-discard").click()
    expect(rows).to_have_count(1)
    assert discards == ["DELETE"]
    expect(chip).to_have_text("1 queued")

    # The agent takes the other one: the transcript shows it, and the sheet
    # empties without waiting for the session-host to notice.
    tr.append(_turn("user", "then open the PR\nbut do not merge", 300))
    _tick(page, tr)
    expect(rows).to_have_count(0)
    expect(page.locator("#pendingInputEmpty")).to_be_visible()
    expect(page.locator("#chatQueued")).to_be_hidden()

    page.locator("#pendingInputDone").click()
    expect(sheet).to_be_hidden()


def test_sending_text_already_pending_asks_first(authed_page: Page, base_url: str) -> None:
    page = authed_page
    tr = _boot(page, base_url)
    tr.pending_input = [_QUEUED]
    sends: list = []
    page.route(
        re.compile(r".*/api/claude-code/sessions/" + _SID + r"/input$"),
        lambda route: (sends.append(1), route.fulfill(status=200, content_type="application/json",
                                                      body=_json.dumps({"ok": True, "submit_state": "confirmed"}))),
    )
    _open_chat(page)
    expect(page.locator("#chatQueuedChip")).to_have_text("1 queued", timeout=OVERLAY_OPEN_MS)

    composer = page.locator("#chatComposeBar")
    composer.locator(".composer-input").fill("also check the ferry times")
    composer.locator(".composer-send").click()

    confirm = page.locator("#confirmDialog")
    expect(confirm).to_be_visible()
    expect(page.locator("#confirmDialogTitle")).to_have_text("Already queued")
    page.locator("#confirmDialogClose").click()
    expect(confirm).to_be_hidden()
    # A "no" sends nothing and keeps the draft.
    expect(composer.locator(".composer-input")).to_have_value("also check the ferry times")
    assert sends == []
