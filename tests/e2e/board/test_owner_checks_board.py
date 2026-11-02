"""Owner checks on the Board: Your turn's cards, their detail, and the answer sheet."""

from __future__ import annotations

import copy
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.board._chief_support import (
    _QUESTIONS, _board_payload, _capture_chief_input, _mock_board, _mock_ensure, _open_board,
    _plan_with, _route_plan_from, _route_voice,
)

pytestmark = pytest.mark.smoke

ARTIFACT = "https://claude.ai/code/artifact/0123abcd"

_OWNER_ITEMS = [
    {"ref": "demo#7", "repo": "demo", "number": 7, "title": "Pair the kitchen speaker",
     "url": "https://github.com/octo/demo/issues/7", "state": "OPEN", "kind": "before-start",
     "block": True, "reason": "needs a real device", "try": "hold the pair button for 5 s",
     "proof": [], "since": "2026-10-30", "mark": None, "mark_at": None, "mark_until": None},
    {"ref": "demo#9", "repo": "demo", "number": 9, "title": "Read-aloud on the lock screen",
     "url": "https://github.com/octo/demo/issues/9", "state": "CLOSED", "kind": "validate-after",
     "block": True, "reason": "a live phone", "try": "lock the phone and tap play",
     "proof": ["https://github.com/octo/demo/pull/12", ARTIFACT], "since": "2026-10-31",
     "mark": None, "mark_at": None, "mark_until": None},
    {"ref": "demo#11", "repo": "demo", "number": 11, "title": "Swipe to archive",
     "url": "https://github.com/octo/demo/issues/11", "state": "CLOSED", "kind": "validate-after",
     "block": False, "reason": "", "try": "", "proof": [], "since": "",
     "mark": None, "mark_at": None, "mark_until": None},
]


def _payload() -> dict:
    payload = _board_payload(with_chief=True)
    payload["owner_checks"] = {
        "available": True, "fetched_at": payload["github"]["fetched_at"], "error": None,
        "refreshing": False, "unknown": [{"repo": "other", "reason": "403"}],
        "items": copy.deepcopy(_OWNER_ITEMS),
    }
    return payload


@pytest.mark.iphone
def test_owner_checks_show_in_your_turn_and_answer_through_the_sheet(
    authed_page: Page, base_url: str
) -> None:
    """Each owner check is a Your turn row (title, ref, age), grouped by
    kind and counted in the lane and the header; its tap unfolds why, what to
    try and the links (the artifact opens the artifact). A label without a
    readable block is listed as "details unknown", never hidden, and an
    unreadable repo is said. The sheet answers them: Validated, a typed
    Problem and Later go to the owner-check route as one batch, then the chief
    hears what landed in one message."""
    _mock_board(authed_page, _payload())
    _mock_ensure(authed_page, {})
    chief_posts = _capture_chief_input(authed_page)
    _route_voice(authed_page, False)
    answers: list = []

    def _answer(route):
        answers.append(route.request.post_data_json)
        results = [{"ref": a["ref"], "answer": a["answer"], "ok": True}
                   for a in route.request.post_data_json["answers"]]
        route.fulfill(json={"results": results, "owner_checks": _payload()["owner_checks"]})

    authed_page.route(re.compile(r".*/api/board/owner-checks/answer$"), _answer)
    _open_board(authed_page, base_url)

    lane = authed_page.locator("#boardColYours")
    expect(lane.locator(".board-count")).to_have_text("3")
    # Owner checks are something that needs you: no "Nothing needs you" above them.
    expect(lane.locator('.board-empty[data-col="your_turn"]')).to_be_hidden()
    expect(authed_page.locator("#boardHeadStatus")).to_contain_text("3")
    groups = lane.locator(".board-owner-group")
    expect(groups).to_have_count(2)
    expect(groups.nth(0).locator(".board-plan-heading")).to_have_text("Before you start it (1)")
    expect(groups.nth(1).locator(".board-plan-heading")).to_have_text("To validate (2)")
    expect(lane.locator(".board-owner-state")).to_contain_text("1 repo unreadable")
    unknown = lane.locator('.board-owner-row[data-ref="demo#11"]')
    expect(unknown.locator(".board-owner-unknown")).to_have_text("details unknown")

    row = lane.locator('.board-owner-row[data-ref="demo#9"]')
    expect(row).to_contain_text("demo#9")
    row.locator(".action-row-main").click()
    detail = row.locator(".board-owner-detail")
    expect(detail).to_contain_text("Why: a live phone")
    expect(detail).to_contain_text("Try: lock the phone and tap play")
    expect(detail.locator('a.board-owner-link[href="' + ARTIFACT + '"]')).to_have_text("Artifact")
    expect(detail.locator("a.board-owner-link")).to_have_text(["Issue #9", "PR #12", "Artifact"])
    box = detail.locator(".board-owner-link").first.bounding_box()
    assert box and box["height"] >= 44, box

    detail.locator(".board-owner-answer").click()
    dialog = authed_page.locator("#chiefAnswersDialog")
    expect(dialog).to_be_visible()
    expect(dialog.locator("#chiefAnswersTitleText")).to_have_text("Owner checks")
    blocks = dialog.locator(".owner-answer")
    expect(blocks).to_have_count(3)
    expect(blocks.nth(2)).to_contain_text("Details unknown")
    done = dialog.locator("#chiefAnswersDone")
    expect(done).to_be_disabled()
    blocks.nth(0).locator('.tr-ask-opt[data-answer="later"]').click()
    blocks.nth(1).locator('.tr-ask-opt[data-answer="validated"]').click()
    blocks.nth(2).locator(".tr-ask-input").fill("the swipe  does nothing")
    expect(done).to_be_enabled()
    done.click()
    expect(dialog).to_be_hidden()
    assert answers == [{"answers": [
        {"ref": "demo#7", "kind": "before-start", "answer": "later"},
        {"ref": "demo#9", "kind": "validate-after", "answer": "validated"},
        {"ref": "demo#11", "kind": "validate-after", "answer": "problem", "text": "the swipe does nothing"},
    ]}], answers
    assert chief_posts == [{"data": (
        "Owner checks answered on the Board:\n"
        "[demo#9] Validated: Read-aloud on the lock screen\n"
        "[demo#11] Problem found validating Swipe to archive: the swipe does nothing (please file a fix)"
    ), "submit": True}], chief_posts


@pytest.mark.iphone
def test_chief_and_owner_checks_open_separate_sheets(
    authed_page: Page, base_url: str, tmp_path
) -> None:
    """The Chief's plan "Answer N questions" opens a sheet of the chief's open
    questions alone; an owner-check row's Answer and the group's "Answer N
    owner checks" open a sheet of owner checks alone, scrolled to the tapped
    item, without Anything else. A Problem there still reaches the chief, in
    the owner-checks message only. The group's state line leads its block, where
    the plan card puts its age line."""
    _mock_board(authed_page, _payload())
    _route_plan_from(authed_page, {"plan": _plan_with(tmp_path, _QUESTIONS)})
    _mock_ensure(authed_page, {})
    chief_posts = _capture_chief_input(authed_page)
    _route_voice(authed_page, False)

    def _answer(route):
        results = [{"ref": a["ref"], "answer": a["answer"], "ok": True}
                   for a in route.request.post_data_json["answers"]]
        route.fulfill(json={"results": results, "owner_checks": _payload()["owner_checks"]})

    authed_page.route(re.compile(r".*/api/board/owner-checks/answer$"), _answer)
    with authed_page.expect_response(re.compile(r".*/api/status$")):
        _open_board(authed_page, base_url)

    owner = authed_page.locator("#boardColYours .board-owner")
    expect(owner.locator(":scope > *").first).to_have_class(re.compile(r"\bboard-owner-state\b"))

    dialog = authed_page.locator("#chiefAnswersDialog")
    authed_page.locator("#boardChiefPlan .board-plan-answer").click()
    expect(dialog.locator("#chiefAnswersTitleText")).to_have_text("Answer the chief")
    expect(dialog.locator(".chief-answer")).to_have_count(3)
    expect(dialog.locator(".owner-answer")).to_have_count(0)
    expect(dialog.locator("#chiefAnswersAlso")).to_be_visible()
    dialog.locator("#chiefAnswersClose").click()
    expect(dialog).to_be_hidden()

    owner.locator(".board-owner-answer-all").click()
    expect(dialog.locator("#chiefAnswersTitleText")).to_have_text("Owner checks")
    expect(dialog.locator(".chief-answer")).to_have_count(3)
    expect(dialog.locator(".chief-answer:not(.owner-answer)")).to_have_count(0)
    expect(dialog.locator("#chiefAnswersAlso")).to_be_hidden()
    dialog.locator("#chiefAnswersClose").click()
    expect(dialog).to_be_hidden()

    row = owner.locator('.board-owner-row[data-ref="demo#11"]')
    row.locator(".action-row-main").click()
    row.locator(".board-owner-answer").click()
    expect(dialog.locator("#chiefAnswersTitleText")).to_have_text("Owner checks")
    expect(dialog.locator('[data-key="owner:demo#11"]')).to_be_in_viewport()
    dialog.locator('[data-key="owner:demo#11"] .tr-ask-input').fill("the swipe does nothing")
    dialog.locator("#chiefAnswersDone").click()
    expect(dialog).to_be_hidden()
    assert chief_posts == [{"data": (
        "Owner checks answered on the Board:\n"
        "[demo#11] Problem found validating Swipe to archive: the swipe does nothing (please file a fix)"
    ), "submit": True}], chief_posts
