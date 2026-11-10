"""The Board's Ideas card and an idea's sheet: read, edit, attach, file, and the unknown state.

Hermetic: /api/inspiration/ideas is mocked with made-up ideas, the chief's
input and upload routes are captured, so nothing reaches a real chief or the
owner's real ideas. Covered: the card's count and its Applied / Filtered
ledger, the sheet's attribution and repo suggestion, an edited prompt and an
attached file sent as one contract message, the toast, the row's "Sent" mark
and the sheet's outcome in place of a second send, then an unreadable file
drawn as unknown, never as zero ideas.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.board._chief_support import (
    _CHIEF_UPLOAD,
    _board_payload,
    _capture_chief_input,
    _mock_board,
    _mock_chief_upload,
    _mock_ensure,
    _open_board,
    _PNG_1x1,
    _route_voice,
)

pytestmark = pytest.mark.smoke

_IDEA = {
    "id": "i-one", "title": "Cap file length with a lint rule", "link": "https://example.test/post/1",
    "source": {"name": "Demo feed", "kind": "x"}, "author": "@demo_author",
    "published": "2026-11-08T10:00:00Z", "excerpt": "write the lint rule first", "digest": "2026-11-10",
    "score": 9, "model": "demo-model", "why_fits": "Small repos stay small.", "experiment": "Try two repos.",
    "suggested_repo": "demo-repo", "prompt": "Lint: cap file length\n\n## Source\n- Link: https://example.test/post/1",
}


def _payload() -> dict:
    return {
        "state": "ok", "reason": "", "digest": "2026-11-10",
        "ideas": [_IDEA, dict(_IDEA, id="i-two", title="Second made-up idea", score=7)],
        "ledger": [
            dict(_IDEA, id="i-done", state="applied", at="2026-11-09T08:00:00Z", issue="demo-repo#7",
                 issue_url="https://github.com/octo/demo-repo/issues/7", note=""),
            dict(_IDEA, id="i-no", state="filtered", at="2026-11-09T09:00:00Z", issue="", issue_url="",
                 note="not this month"),
        ],
        "sources": {"state": "ok", "date": "2026-11-10", "chips": [{"text": "YouTube 0/1 read", "ok": False}]},
    }


def test_ideas_card_files_an_edited_idea_once_and_shows_unknown(authed_page: Page, base_url: str) -> None:
    page = authed_page
    served = {"body": _payload()}
    _mock_board(page, _board_payload(with_chief=True))
    page.route(re.compile(r".*/api/inspiration/ideas$"), lambda route: route.fulfill(json=served["body"]))
    _mock_ensure(page, {})
    posts = _capture_chief_input(page)
    _mock_chief_upload(page)
    _route_voice(page, True)
    _open_board(page, base_url)

    # The card: two new ideas, and the ledger behind its segments.
    card = page.locator("#boardIdeas")
    expect(card.locator(".board-ideas-count")).to_have_text("2")
    expect(card.locator(".board-idea .action-row-title")).to_have_text(
        ["Cap file length with a lint rule", "Second made-up idea"])
    expect(card.locator(".board-idea").first).to_contain_text("9/10 · X · @demo_author · 8 Nov")
    expect(card.locator(".board-ideas-line")).to_contain_text("YouTube 0/1 read")
    card.locator('.segmented-item[data-value="applied"]').click()
    expect(card.locator(".board-idea .board-idea-issue")).to_have_text("demo-repo#7")
    card.locator('.segmented-item[data-value="filtered"]').click()
    expect(card.locator(".board-idea")).to_contain_text("not this month")
    card.locator('.segmented-item[data-value="new"]').click()

    # The sheet: attribution, the suggested repo, an edited prompt and a file.
    card.locator(".board-idea .action-row-main").first.click()
    sheet = page.locator("#ideaSheetDialog")
    expect(sheet.locator(".idea-sheet-title")).to_have_text(_IDEA["title"])
    expect(sheet.locator(".idea-sheet-attr")).to_contain_text("inspiration scan · digest 2026-11-10 · scored by demo-model")
    expect(sheet.locator(".idea-sheet-repo")).to_have_value("demo-repo")
    expect(sheet.locator(".idea-sheet-tools .tr-ask-mic")).to_be_visible()
    sheet.locator(".idea-sheet-prompt").fill("Lint: cap file length at 800\n\n## Source\n- Link: x")
    sheet.locator('input[type="file"]').set_input_files(
        files=[{"name": "e2e-stub-shot.png", "mimeType": "image/png", "buffer": _PNG_1x1}])
    expect(sheet.locator(".tr-ask-file")).to_have_count(1)
    sheet.locator("#ideaSheetFile").click()
    expect(sheet).to_be_hidden()
    expect(page.locator("#toast")).to_contain_text("Sent to the chief")
    assert posts == [{
        "data": ("Idea from the Board: i-one -> file in demo-repo\n"
                 "Lint: cap file length at 800\n\n## Source\n- Link: x\n"
                 "Attached: " + _CHIEF_UPLOAD),
        "submit": True,
    }], posts

    # Sent: the row says so, and the sheet shows the outcome with no second send.
    expect(card.locator(".board-idea").first.locator(".board-idea-sent")).to_have_text("Sent")
    card.locator(".board-idea .action-row-main").first.click()
    expect(sheet.locator(".idea-sheet-outcome")).to_contain_text("Sent to the chief to file.")
    expect(page.locator("#ideaSheetActions")).to_be_hidden()
    sheet.locator("#ideaSheetClose").click()
    assert len(posts) == 1

    # An unreadable file is its own unknown state, never zero ideas.
    served["body"] = {"state": "unknown", "reason": "ideas.json is not valid JSON.", "digest": "",
                      "ideas": [], "ledger": [], "sources": None}
    page.reload(wait_until="domcontentloaded")
    _open_board(page, base_url)
    expect(card.locator(".board-ideas-line")).to_have_text("Ideas unknown: ideas.json is not valid JSON.")
    expect(card.locator(".board-ideas-count")).to_have_text("—")
