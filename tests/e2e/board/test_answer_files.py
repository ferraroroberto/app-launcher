"""Files on the Board answer sheets: a paperclip on every item, in both sheets.

A chief answer carries each stored path under its item's line. An owner-check
answer posts to GitHub only a count of files; the paths reach the chief's
terminal. Every file here is a synthetic stub (``e2e-stub-...``).
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.board._chief_support import (
    _PNG_1x1, _QUESTIONS, _board_payload, _capture_chief_input, _mock_board, _mock_ensure,
    _open_board, _plan_with, _route_plan_from, _route_voice,
)
from tests.e2e.board.test_owner_checks_board import _payload

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_STORED = "E:/automation/fleet-config/.launcher-tmp/e2e-stub-stored-{n}.png"


def _stub(name: str) -> dict:
    return {"name": name, "mimeType": "image/png", "buffer": _PNG_1x1}


def _numbered_uploads(page: Page) -> list:
    """Answer the chief session's upload route with a new stored path each time."""
    urls: list = []

    def _store(route):
        urls.append(route.request.url)
        route.fulfill(json={"path": _STORED.format(n=len(urls))})

    page.route(re.compile(r".*/api/claude-code/sessions/s-chief/image(?:\?.*)?$"), _store)
    return urls


def _attach(block, *names: str) -> None:
    block.locator('input[type="file"]').set_input_files(files=[_stub(n) for n in names])


def test_chief_answer_carries_each_files_path_under_its_line(
    authed_page: Page, base_url: str, tmp_path
) -> None:
    _mock_board(authed_page, _board_payload(with_chief=True))
    _route_plan_from(authed_page, {"plan": _plan_with(tmp_path, _QUESTIONS)})
    _mock_ensure(authed_page, {})
    posts = _capture_chief_input(authed_page)
    uploads = _numbered_uploads(authed_page)
    _route_voice(authed_page, False)
    with authed_page.expect_response(re.compile(r".*/api/status$")):
        _open_board(authed_page, base_url)

    authed_page.locator("#boardChiefPlan .board-plan-answer").click()
    dialog = authed_page.locator("#chiefAnswersDialog")
    blocks = dialog.locator(".chief-answer")
    first, second = blocks.nth(0), blocks.nth(1)
    clip = first.locator(".tr-ask-attach")
    expect(clip).to_be_enabled()
    box = clip.bounding_box()
    assert box and box["height"] >= 44 and box["width"] >= 44, box
    expect(clip).to_have_attribute("data-track", "board.answer-attach")

    first.locator(".tr-ask-opt").nth(0).click()
    _attach(first, "e2e-stub-a.png", "e2e-stub-b.png")
    chips = first.locator(".tr-ask-file")
    expect(chips).to_have_count(2)
    expect(chips.nth(0)).to_contain_text("e2e-stub-a.png")
    # A chip comes off; its file is no longer in the message.
    chips.nth(1).locator(".tr-ask-file-remove").click()
    expect(chips).to_have_count(1)
    # Files alone answer the second question; the third has neither and is skipped.
    expect(second.locator(".tr-ask-file")).to_have_count(0)
    _attach(second, "e2e-stub-c.png")
    expect(second.locator(".tr-ask-file")).to_have_count(1)
    assert len(uploads) == 3, uploads

    dialog.locator("#chiefAnswersDone").click()
    expect(dialog).to_be_hidden()
    assert posts == [{
        "data": (
            "Answers from the Board (2 of 3):\n"
            "1. [fleet-config#959] Approve the four plans? → Yes, all four (recommended) {id: q-plans}\n"
            "   Attached: " + _STORED.format(n=1) + "\n"
            "2. [life-os] Which days work? → (files only) {id: q-days}\n"
            "   Attached: " + _STORED.format(n=3) + "\n"
            "Skipped: 3"
        ),
        "submit": True,
    }], posts


def test_owner_check_files_stay_off_github_and_reach_the_chief(
    authed_page: Page, base_url: str, tmp_path
) -> None:
    _mock_board(authed_page, _payload())
    _route_plan_from(authed_page, {"plan": _plan_with(tmp_path, _QUESTIONS)})
    _mock_ensure(authed_page, {})
    chief_posts = _capture_chief_input(authed_page)
    _numbered_uploads(authed_page)
    _route_voice(authed_page, False)
    bodies: list = []

    def _answer(route):
        bodies.append(route.request.post_data)
        results = [{"ref": a["ref"], "answer": a["answer"], "ok": True}
                   for a in route.request.post_data_json["answers"]]
        route.fulfill(json={"results": results, "owner_checks": _payload()["owner_checks"]})

    authed_page.route(re.compile(r".*/api/board/owner-checks/answer$"), _answer)
    with authed_page.expect_response(re.compile(r".*/api/status$")):
        _open_board(authed_page, base_url)

    owner = authed_page.locator("#boardColYours .board-owner")
    dialog = authed_page.locator("#chiefAnswersDialog")
    # Each group is its own sheet and its own message to the chief.
    owner.locator('.board-owner-group[data-kind="before-start"] .board-owner-answer-group').click()
    before = dialog.locator('[data-key="owner:demo#7"]')
    # A before-start step has the Other field a chief question has, its paperclip beside it.
    expect(before.locator(".tr-ask-other .tr-ask-input")).to_have_count(1)
    expect(before.locator(".tr-ask-attach")).to_be_enabled()

    # Files with no answer send nothing: Done waits for the pick.
    _attach(before, "e2e-stub-a.png")
    expect(before.locator(".tr-ask-file")).to_have_count(1)
    expect(dialog.locator("#chiefAnswersDone")).to_be_disabled()
    before.locator('.tr-ask-opt[data-answer="done"]').click()
    dialog.locator("#chiefAnswersDone").click()
    expect(dialog).to_be_hidden()

    owner.locator('.board-owner-group[data-kind="validate-after"] .board-owner-answer-group').click()
    validate = dialog.locator('[data-key="owner:demo#9"]')
    validate.locator(".tr-ask-input").fill("blank tab")
    _attach(validate, "e2e-stub-b.png")
    expect(validate.locator(".tr-ask-file")).to_have_count(1)
    dialog.locator("#chiefAnswersDone").click()
    expect(dialog).to_be_hidden()

    assert len(bodies) == 2
    assert all("e2e-stub" not in b and "launcher-tmp" not in b for b in bodies), bodies
    assert [authed_page.evaluate("(b) => JSON.parse(b).answers", b) for b in bodies] == [
        [{"ref": "demo#7", "kind": "before-start", "answer": "done", "attachments": 1}],
        [{"ref": "demo#9", "kind": "validate-after", "answer": "problem", "text": "blank tab",
          "attachments": 1}],
    ]
    assert chief_posts == [
        {"data": (
            "Owner checks answered on the Board: Before start\n"
            "[demo#7] Before-start step done: Pair the kitchen speaker\n"
            "   Attached: " + _STORED.format(n=1)
        ), "submit": True},
        {"data": (
            "Owner checks answered on the Board: Validate\n"
            "[demo#9] Problem found validating Read-aloud on the lock screen: blank tab (please file a fix)\n"
            "   Attached: " + _STORED.format(n=2)
        ), "submit": True},
    ], chief_posts


def test_the_paperclip_waits_for_a_running_chief_and_says_so(
    authed_page: Page, base_url: str
) -> None:
    payload = _board_payload(with_chief=False)
    payload["owner_checks"] = _payload()["owner_checks"]
    _mock_board(authed_page, payload)
    _route_voice(authed_page, False)
    with authed_page.expect_response(re.compile(r".*/api/status$")):
        _open_board(authed_page, base_url)
    authed_page.locator(
        '#boardColYours .board-owner-group[data-kind="before-start"] .board-owner-answer-group').click()
    block = authed_page.locator('#chiefAnswersDialog [data-key="owner:demo#7"]')
    expect(block.locator(".tr-ask-attach")).to_be_disabled()
    expect(block.locator(".tr-ask-files-note")).to_have_text("Start the chief to attach files.")
    # Answers that need no file still go without a chief.
    block.locator('.tr-ask-opt[data-answer="pending"]').click()
    expect(authed_page.locator("#chiefAnswersDone")).to_be_enabled()
