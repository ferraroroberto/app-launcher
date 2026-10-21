"""#1745 — a capped prompt or reply loads the rest of itself in place.

A very long turn arrives capped with a note that sent the reader to the
terminal. It is now a **Show the rest** button that fetches #985's
``/transcript/entry`` and swaps the block's text for the full one, so the Chat
view never has to be left. Pinned here, against a stubbed page (every text
synthetic):

* a capped prompt (plain text) and a capped reply (markdown) each offer the
  button at the 44px tap floor; the tap draws the full text in the block it
  was under, the button goes, and the Chat view stays up;
* an expanded entry stays expanded across the live poll, including the tick
  that rebuilds the provisional tail it sits in (the fetched text is
  remembered, the entry is not re-capped and not fetched again);
* a failed fetch keeps the capped text, says so in place and leaves the button
  to try again;
* with nothing to fetch (no offset, a plan card) the note stays the plain one.

Every boot fetch is stubbed before ``goto()`` (#510) and the poll runs on the
fake clock (#1376).
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import _fake_clock
from tests.e2e.conftest import OVERLAY_OPEN_MS, stable_read
from tests.e2e.test_chat_live_refresh import (
    _SID,
    _Transcript,
    _mock_sessions_list,
    _open_chat,
    _plan,
    _tick,
    _turn,
)

pytestmark = pytest.mark.smoke

_CAPPED_NOTE = "(truncated — open the terminal for the rest)"
_FAILED = "Couldn’t load the rest — try again"

_PROMPT_CAPPED = "start of a very long prompt " + "p" * 60
_PROMPT_FULL = _PROMPT_CAPPED + " and then the rest of it, <b>never</b> markup"
_REPLY_CAPPED = "start of a very long reply " + "r" * 60
_REPLY_FULL = _REPLY_CAPPED + "\n\nand then **the rest** of it"


class _Entries:
    """A stub of ``/transcript/entry``: full text by offset, every call kept.

    ``fail`` makes the next that many answers unavailable (a rotated source).
    """

    def __init__(self, page: Page, texts: dict[int, str]) -> None:
        self.texts = texts
        self.calls: list[str] = []
        self.fail = 0
        page.route(
            re.compile(r".*/api/claude-code/sessions/" + _SID + r"/transcript/entry(\?.*)?$"),
            self._handle,
        )

    def _handle(self, route) -> None:
        self.calls.append(route.request.url)
        if self.fail:
            self.fail -= 1
            body = {"available": False, "reason": "entry_not_found", "session_id": _SID}
        else:
            offset = int(re.search(r"offset=(\d+)", route.request.url).group(1))
            body = {"available": True, "reason": None, "session_id": _SID,
                    "text": self.texts[offset], "truncated": False}
        route.fulfill(status=200, content_type="application/json", body=_json.dumps(body))


def _capped(kind: str, text: str, offset: int | None) -> dict:
    e = _turn(kind, text, offset if offset is not None else 0)
    e["truncated"] = True
    if offset is None:
        del e["offset"]
    return e


def _boot(page: Page, base_url: str, entries: list[dict], texts: dict[int, str]):
    _mock_sessions_list(page)
    tr = _Transcript(page)
    tr.append(*entries)
    stub = _Entries(page, texts)
    _fake_clock.install(page)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_chat(page)
    expect(page.locator("#transcriptList .tr-turn")).not_to_have_count(0, timeout=OVERLAY_OPEN_MS)
    return tr, stub


def _two_capped() -> list[dict]:
    return [_capped("user", _PROMPT_CAPPED, 100), _capped("assistant", _REPLY_CAPPED, 200)]


@pytest.mark.iphone
def test_show_the_rest_loads_a_capped_prompt_and_reply_in_place(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    _tr, stub = _boot(page, base_url, _two_capped(), {100: _PROMPT_FULL, 200: _REPLY_FULL})
    prompt = page.locator("#transcriptList .tr-prompt-item")
    reply = page.locator("#transcriptList .tr-reply")

    # The button replaces the dead-end note, at the fleet tap floor.
    for scope in (prompt, reply):
        expect(scope.locator(".tr-trunc-more")).to_have_text("Show the rest")
        expect(scope).not_to_contain_text(_CAPPED_NOTE)
    box = stable_read(lambda: reply.locator(".tr-trunc-more").bounding_box())
    assert box is not None and box["height"] >= 44, f"Show the rest under the 44px floor: {box}"

    # A prompt is plain text: the rest lands as text, its markup never parsed.
    prompt.locator(".tr-trunc-more").click()
    expect(prompt.locator(".tr-text")).to_have_text(_PROMPT_FULL)
    expect(prompt.locator(".tr-text b")).to_have_count(0)
    expect(prompt.locator(".tr-trunc")).to_have_count(0)

    # A reply is markdown, as it was before it was capped.
    reply.locator(".tr-trunc-more").click()
    expect(reply.locator(".tr-md strong")).to_have_text("the rest")
    expect(reply.locator(".tr-trunc")).to_have_count(0)

    assert [re.search(r"offset=(\d+)", u).group(1) for u in stub.calls] == ["100", "200"], stub.calls
    # Never left Chat for the terminal.
    expect(page.locator("#transcriptList")).to_be_visible()


def test_an_expanded_entry_stays_expanded_across_the_poll(authed_page: Page, base_url: str) -> None:
    page = authed_page
    tr, stub = _boot(page, base_url, _two_capped(), {100: _PROMPT_FULL, 200: _REPLY_FULL})
    reply = page.locator("#transcriptList .tr-reply").first
    reply.locator(".tr-trunc-more").click()
    expect(reply.locator(".tr-md strong")).to_have_text("the rest")

    # A tick that brings a new fragment rebuilds the provisional tail, which
    # the newest reply sat in: it is drawn expanded again, not re-capped.
    tr.append(_turn("assistant", "a later fragment", 300))
    _tick(page, tr)
    expect(page.locator("#transcriptList")).to_contain_text("a later fragment", timeout=OVERLAY_OPEN_MS)
    first = page.locator("#transcriptList .tr-reply").first
    expect(first.locator(".tr-md strong")).to_have_text("the rest")
    expect(first).not_to_contain_text(_CAPPED_NOTE)
    expect(first.locator(".tr-trunc-more")).to_have_count(0)

    # A tick with nothing new changes nothing either, and nothing was refetched.
    _tick(page, tr)
    expect(first.locator(".tr-md strong")).to_have_text("the rest")
    assert len(stub.calls) == 1, stub.calls

    # The still-capped prompt kept its button through all of it.
    expect(page.locator("#transcriptList .tr-prompt-item .tr-trunc-more")).to_have_count(1)


def test_a_failed_fetch_keeps_the_capped_text_and_offers_a_retry(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    _tr, stub = _boot(page, base_url, _two_capped(), {100: _PROMPT_FULL, 200: _REPLY_FULL})
    reply = page.locator("#transcriptList .tr-reply")
    more = reply.locator(".tr-trunc-more")

    stub.fail = 1
    more.click()
    expect(reply.locator(".tr-trunc-status")).to_have_text(_FAILED)
    expect(reply.locator(".tr-md")).to_contain_text(_REPLY_CAPPED)
    expect(reply.locator(".tr-md")).not_to_contain_text("and then")
    expect(more).to_be_enabled()
    expect(more).to_have_text("Show the rest")

    # Trying again works, and the message goes with the note.
    more.click()
    expect(reply.locator(".tr-md strong")).to_have_text("the rest")
    expect(reply.locator(".tr-trunc")).to_have_count(0)
    assert len(stub.calls) == 2, stub.calls


def test_a_capped_block_with_nothing_to_fetch_keeps_the_plain_note(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    plan = _plan("call-plan-1745", 300)
    plan["plan_truncated"] = True
    _boot(
        page, base_url,
        [_capped("user", _PROMPT_CAPPED, None), _capped("assistant", _REPLY_CAPPED, 200), plan],
        {},
    )
    # No offset: nothing to ask the server for.
    prompt = page.locator("#transcriptList .tr-prompt-item")
    expect(prompt.locator(".tr-trunc")).to_have_text(_CAPPED_NOTE)
    expect(prompt.locator(".tr-trunc-more")).to_have_count(0)
    # A plan's text is not a turn the entry route serves.
    card = page.locator("#transcriptList .tr-plan")
    expect(card.locator(".tr-trunc")).to_have_text(_CAPPED_NOTE)
    expect(card.locator(".tr-trunc-more")).to_have_count(0)
    # The reply beside them, which does have an offset, still offers it.
    expect(page.locator("#transcriptList .tr-reply .tr-trunc-more")).to_have_count(1)
