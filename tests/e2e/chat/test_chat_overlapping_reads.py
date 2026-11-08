"""Chat live refresh: a refresh nudge that lands while a read is in flight waits for it, so no turn is appended twice."""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import _fake_clock
from tests.e2e.chat._support import _SID, _boot, _open_chat, _question, _tick, _turn
from tests.e2e.fixtures.waits import OVERLAY_OPEN_MS

pytestmark = pytest.mark.smoke

# session-transcript.js SENT_REFRESH_MS: how long after an answer the pane
# nudges the live poll.
_SENT_REFRESH_MS = 3_000


def test_a_nudge_during_a_read_does_not_append_twice(authed_page: Page, base_url: str) -> None:
    page = authed_page
    tr = _boot(page, base_url)
    page.route(
        re.compile(r".*/api/claude-code/sessions/" + _SID + r"/answer$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"ok": True, "delivered": "unconfirmed", "steps": 1}),
        ),
    )
    _open_chat(page)
    tr.append(_turn("assistant", "one question first", 250), _question("toolu_q", 300))
    _tick(page, tr)
    card = page.locator('#transcriptList .tr-ask-item[data-call-id="toolu_q"]')
    expect(card).to_have_attribute("data-mode", "live", timeout=OVERLAY_OPEN_MS)

    # One read is held open; the answer's refresh nudge fires while it is.
    # Against a real phone this is a slow tick that is still on the wire when
    # the nudge lands.
    tr.append(_turn("assistant", "only once please", 400), _turn("assistant", "tail", 450))
    tr.hold = True
    _tick(page, tr)
    with page.expect_response(re.compile(r".*/answer$")):
        card.locator(".tr-ask-opt").first.click()
    expect(page.locator(".toast", has_text="Answer sent")).to_be_visible()
    _fake_clock.advance(page, _SENT_REFRESH_MS + 1)
    # The nudge has not started a second read from the same cursor.
    assert len(tr.held) == 1, tr.calls[-3:]
    tr.release()
    _fake_clock.advance(page, 1)
    # A nudge waited out the held read and then read from its cursor, if at
    # all; either way the release left nothing more held.
    tr.release()
    _fake_clock.advance(page, 1)

    expect(page.locator("#transcriptList")).to_contain_text("only once please")
    expect(page.locator("#transcriptList .tr-reply", has_text="only once please")).to_have_count(1)
    expect(card).to_have_count(1)
