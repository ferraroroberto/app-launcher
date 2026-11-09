"""Owner checks on the Board: Your turn's cards, their detail, and the answer sheet."""

from __future__ import annotations

import copy
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.board._chief_support import (
    _QUESTIONS,
    _RECORDER_MOCK,
    _board_payload,
    _capture_chief_input,
    _mock_board,
    _mock_ensure,
    _open_board,
    _plan_with,
    _route_plan_from,
    _route_voice,
)
from tests.e2e.fixtures.waits import stable_eval, stable_read

pytestmark = pytest.mark.smoke

ARTIFACT = "https://claude.ai/code/artifact/0123abcd"

_OWNER_ITEMS = [
    {"ref": "demo#7", "repo": "demo", "number": 7, "title": "Pair the kitchen speaker",
     "url": "https://github.com/octo/demo/issues/7", "state": "OPEN", "kind": "before-start",
     "block": True, "reason": "needs a real device", "try": "hold the pair button for 5 s",
     "proof": [], "since": "2026-10-30", "mark": None, "mark_at": None, "mark_until": None},
    {"ref": "demo#9", "repo": "demo", "number": 9, "title": "Read-aloud on the lock screen",
     "url": "https://github.com/octo/demo/issues/9", "state": "OPEN", "kind": "validate-after",
     "block": True, "reason": "a live phone", "try": "lock the phone and tap play",
     "proof": ["https://github.com/octo/demo/pull/12", ARTIFACT], "since": "2026-10-31",
     "mark": None, "mark_at": None, "mark_until": None},
    {"ref": "demo#11", "repo": "demo", "number": 11, "title": "Swipe to archive",
     "url": "https://github.com/octo/demo/issues/11", "state": "OPEN", "kind": "validate-after",
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


def _open_owner_board(authed_page: Page, base_url: str) -> tuple[list, list]:
    """Open the Board on the owner-check payload with the answer route stubbed
    to apply every answer; returns the posts it saw and the chief's input."""
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
    return answers, chief_posts


@pytest.mark.iphone
def test_owner_checks_show_in_your_turn_and_answer_through_the_sheet(
    authed_page: Page, base_url: str
) -> None:
    """Each owner check is a Your turn row (title, ref, age), grouped by
    kind and counted in the lane and the header; its tap unfolds why, what to
    try and the links (the artifact opens the artifact). A label without a
    readable block is listed as "details unknown", never hidden, and an
    unreadable repo is said. The sheet answers them: Validated and a typed
    Problem go to the owner-check route as one batch, then the chief
    hears what landed in one message."""
    answers, chief_posts = _open_owner_board(authed_page, base_url)

    lane = authed_page.locator("#boardColYours")
    expect(lane.locator(".board-count")).to_have_text("3")
    # Owner checks are something that needs you: no "Nothing needs you" above them.
    expect(lane.locator('.board-empty[data-col="your_turn"]')).to_be_hidden()
    expect(authed_page.locator("#boardHeadStatus")).to_contain_text("3")
    groups = lane.locator(".board-owner-group")
    expect(groups).to_have_count(2)
    expect(groups.nth(0).locator(".board-plan-heading")).to_have_text("Before you start (1)")
    expect(groups.nth(1).locator(".board-plan-heading")).to_have_text("Validate to close (2)")
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
    # A row's Answer opens its own group's sheet: Validate holds two of the three.
    expect(dialog.locator("#chiefAnswersTitleText")).to_have_text("Owner checks: Validate to close")
    blocks = dialog.locator(".owner-answer")
    expect(blocks).to_have_count(2)
    expect(blocks.nth(1)).to_contain_text("Details unknown")
    done = dialog.locator("#chiefAnswersDone")
    expect(done).to_be_disabled()
    # Newest first: demo#9 (filed 10-31) leads the undated demo#11.
    blocks.nth(0).locator('.tr-ask-opt[data-answer="validated"]').click()
    blocks.nth(1).locator(".tr-ask-input").fill("the swipe  does nothing")
    expect(done).to_be_enabled()
    done.click()
    expect(dialog).to_be_hidden()
    assert answers == [{"answers": [
        {"ref": "demo#9", "kind": "validate-after", "answer": "validated"},
        {"ref": "demo#11", "kind": "validate-after", "answer": "problem", "text": "the swipe does nothing"},
    ]}], answers
    # The chief hears which group the answers came from.
    assert chief_posts == [{"data": (
        "Owner checks answered on the Board: Validate to close\n"
        "[demo#9] Validated: Read-aloud on the lock screen\n"
        "[demo#11] Problem found validating Swipe to archive: the swipe does nothing (please file a fix)"
    ), "submit": True}], chief_posts

    # The Before start group's button opens a sheet of that group alone; its
    # answer goes to the route by itself, and the chief hears a Pending as a line.
    answers.clear()
    chief_posts.clear()
    lane.locator(".board-owner-answer-group").first.click()
    expect(dialog.locator("#chiefAnswersTitleText")).to_have_text("Owner checks: Before start")
    expect(dialog.locator(".owner-answer")).to_have_count(1)
    dialog.locator('.tr-ask-opt[data-answer="pending"]').click()
    dialog.locator("#chiefAnswersDone").click()
    expect(dialog).to_be_hidden()
    assert answers == [{"answers": [{"ref": "demo#7", "kind": "before-start", "answer": "pending"}]}], answers
    assert chief_posts == [{"data": (
        "Owner checks answered on the Board: Before start\n"
        "[demo#7] Before-start: pending - Pair the kitchen speaker"
    ), "submit": True}], chief_posts


@pytest.mark.iphone
def test_chief_and_owner_checks_open_separate_sheets(
    authed_page: Page, base_url: str, tmp_path
) -> None:
    """The Chief's plan "Answer N questions" opens a sheet of the chief's open
    questions alone; an owner-check row's Answer and the group's "Answer N
    owner checks" open a sheet of owner checks alone, scrolled to the tapped
    item, with Anything else under it. A Problem there still reaches the chief, in
    the owner-checks message only. The group's state line leads its block, where
    the plan card puts its age line, and each group folds on its own."""
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

    # Both groups fold on the plan card's summary row, open by default; a
    # folded group keeps its heading and count and hides its rows and answer
    # button, and the choice survives the Board's rebuilds.
    before = owner.locator('.board-owner-group[data-group="before_start"]')
    validate = owner.locator('.board-owner-group[data-group="validate_to_close"]')
    for group in (before, validate):
        expect(group).to_have_attribute("open", "")
        box = stable_read(group.locator("summary").bounding_box)
        assert box and box["height"] >= 44, f"fold row is below a 44px target: {box}"
    expect(before.locator("li.board-owner-row")).to_have_count(1)

    before.locator("summary").click()
    expect(before.locator("li.board-owner-row")).to_be_hidden()
    expect(before.locator(".board-owner-answer-group")).to_be_hidden()
    expect(before.locator("summary")).to_have_text("Before you start (1)")
    # The other group is untouched, and a re-render keeps the fold.
    expect(validate.locator("li.board-owner-row").first).to_be_visible()
    validate.locator('.board-owner-row[data-ref="demo#9"] .action-row-main').click()
    expect(validate.locator(".board-owner-detail")).to_be_visible()
    expect(before.locator("li.board-owner-row")).to_be_hidden()
    before.locator("summary").click()
    expect(before.locator("li.board-owner-row")).to_be_visible()

    dialog = authed_page.locator("#chiefAnswersDialog")
    authed_page.locator("#boardChiefPlan .board-plan-answer").click()
    expect(dialog.locator("#chiefAnswersTitleText")).to_have_text("Answer the chief")
    expect(dialog.locator(".chief-answer")).to_have_count(3)
    expect(dialog.locator(".owner-answer")).to_have_count(0)
    expect(dialog.locator("#chiefAnswersAlso")).to_be_visible()
    dialog.locator("#chiefAnswersClose").click()
    expect(dialog).to_be_hidden()

    # Each group's button opens only that group's checks.
    before_btn = owner.locator('.board-owner-group[data-group="before_start"] .board-owner-answer-group')
    validate_btn = owner.locator('.board-owner-group[data-group="validate_to_close"] .board-owner-answer-group')
    expect(before_btn).to_have_text("Answer Before start (1)")
    expect(validate_btn).to_have_text("Answer Validate to close (2)")
    before_btn.click()
    expect(dialog.locator("#chiefAnswersTitleText")).to_have_text("Owner checks: Before start")
    expect(dialog.locator(".chief-answer")).to_have_count(1)
    expect(dialog.locator('[data-key="owner:demo#7"]')).to_be_visible()
    dialog.locator("#chiefAnswersClose").click()
    expect(dialog).to_be_hidden()
    validate_btn.click()
    expect(dialog.locator("#chiefAnswersTitleText")).to_have_text("Owner checks: Validate to close")
    expect(dialog.locator(".chief-answer")).to_have_count(2)
    expect(dialog.locator(".chief-answer:not(.owner-answer)")).to_have_count(0)
    expect(dialog.locator("#chiefAnswersAlso")).to_be_visible()
    dialog.locator("#chiefAnswersClose").click()
    expect(dialog).to_be_hidden()

    row = owner.locator('.board-owner-row[data-ref="demo#11"]')
    row.locator(".action-row-main").click()
    row.locator(".board-owner-answer").click()
    expect(dialog.locator("#chiefAnswersTitleText")).to_have_text("Owner checks: Validate to close")
    expect(dialog.locator('[data-key="owner:demo#11"]')).to_be_in_viewport()
    dialog.locator('[data-key="owner:demo#11"] .tr-ask-input').fill("the swipe does nothing")
    dialog.locator("#chiefAnswersDone").click()
    expect(dialog).to_be_hidden()
    assert chief_posts == [{"data": (
        "Owner checks answered on the Board: Validate to close\n"
        "[demo#11] Problem found validating Swipe to archive: the swipe does nothing (please file a fix)"
    ), "submit": True}], chief_posts


def test_an_old_check_quotes_the_issue_or_says_it_has_no_try(
    authed_page: Page, base_url: str
) -> None:
    """A check whose recorded Try was the back-fill boilerplate shows the lines
    the issue's own checklist offers, labelled as quoted, or a muted "No specific
    Try on record" when it offers none; a written Try is shown as it always was."""
    payload = _payload()
    old = {"block": True, "reason": "Shipped with no owner check on record", "try": "",
           "try_generic": True, "try_lines": [], "try_from": ""}
    payload["owner_checks"]["items"] = [
        dict(_OWNER_ITEMS[1], **{**old, "try_from": "Acceptance criteria",
                                 "try_lines": ["Open the tab", "The badge counts one"]}),
        dict(_OWNER_ITEMS[2], **old),
    ]
    _mock_board(authed_page, payload)
    _mock_ensure(authed_page, {})
    _route_voice(authed_page, False)
    _open_board(authed_page, base_url)

    lane = authed_page.locator("#boardColYours")
    quoted = lane.locator('.board-owner-row[data-ref="demo#9"]')
    quoted.locator(".action-row-main").click()
    detail = quoted.locator(".board-owner-detail")
    expect(detail).to_contain_text("Try (from the issue's acceptance criteria):")
    expect(detail.locator(".board-owner-try-list li")).to_have_text(["Open the tab", "The badge counts one"])
    expect(detail).not_to_contain_text("Use what this issue shipped")

    bare = lane.locator('.board-owner-row[data-ref="demo#11"]')
    bare.locator(".action-row-main").click()
    expect(bare.locator(".board-owner-try-none")).to_have_text(
        "No specific Try on record. Open the issue to see what it shipped."
    )


def _many_owner_items(n: int) -> list:
    """`n` synthetic owner checks, every third one before-start."""
    items = []
    for i in range(n):
        kind = "before-start" if i % 3 == 0 else "validate-after"
        items.append({
            "ref": f"demo#{100 + i}", "repo": "demo", "number": 100 + i, "title": f"Synthetic check {i}",
            "url": f"https://github.com/octo/demo/issues/{100 + i}", "state": "OPEN", "kind": kind,
            "block": True, "reason": "a live phone", "try": "tap it", "proof": [], "since": "2026-10-30",
            "mark": None, "mark_at": None, "mark_until": None,
        })
    return items


@pytest.mark.iphone
def test_a_long_owner_check_group_lists_every_row_with_no_more_row(
    authed_page: Page, base_url: str
) -> None:
    """Twelve checks in one group all render (no "Show N more" fold), the
    heading keeps its count, and the last row scrolls fully above the tab bar."""
    items = _many_owner_items(12)
    for it in items:
        it["kind"] = "validate-after"
    payload = _board_payload(with_chief=True)
    payload["owner_checks"] = {
        "available": True, "fetched_at": payload["github"]["fetched_at"], "error": None,
        "refreshing": False, "unknown": [], "items": items,
    }
    _mock_board(authed_page, payload)
    _mock_ensure(authed_page, {})
    _route_voice(authed_page, False)
    _open_board(authed_page, base_url)

    group = authed_page.locator('#boardColYours .board-owner-group[data-group="validate_to_close"]')
    expect(group.locator("li.board-owner-row")).to_have_count(12)
    expect(group.locator("li")).to_have_count(12)
    expect(group.locator("summary")).to_have_text("Validate to close (12)")
    expect(authed_page.locator(".board-owner-more")).to_have_count(0)

    last = group.locator("li.board-owner-row").nth(11)
    last.scroll_into_view_if_needed()
    expect(last).to_be_in_viewport()
    # On a phone the tab bar is a bottom pill: the last row clears it. The desktop rail is a side column.
    nbox = authed_page.locator("nav.tabs").bounding_box()
    if nbox and nbox["width"] > nbox["height"]:
        box = last.bounding_box()
        assert box and box["y"] + box["height"] <= nbox["y"] + 1, (box, nbox)


def test_done_applies_a_large_sheet_of_tapped_and_typed_answers(
    authed_page: Page, base_url: str
) -> None:
    """Forty owner checks answered with taps and typed text all land: Done
    posts them in requests the server's own per-request cap accepts. Typed
    text that only says it works validates the item and keeps the text as a
    note; any other typed text is a problem report, and the field says which
    before Done. An answer the server could not apply keeps its draft and
    shows why in its own block; Done again sends only that one."""
    from app.webapp.routers.board_owner import _MAX_ANSWERS

    # One group per sheet: the whole batch is Validate's.
    items = [dict(it, kind="validate-after") for it in _many_owner_items(40)]
    payload = _board_payload(with_chief=True)
    payload["owner_checks"] = {
        "available": True, "fetched_at": payload["github"]["fetched_at"], "error": None,
        "refreshing": False, "unknown": [], "items": items,
    }
    _mock_board(authed_page, payload)
    _mock_ensure(authed_page, {})
    chief_posts = _capture_chief_input(authed_page)
    _route_voice(authed_page, False)
    posts: list = []
    failing = {"demo#139"}

    def _answer(route):
        answers = route.request.post_data_json["answers"]
        posts.append(answers)
        # The real route's refusal: a request over its cap writes nothing.
        if len(answers) > _MAX_ANSWERS:
            route.fulfill(status=400, json={"detail": f"answers must be a list of 1 to {_MAX_ANSWERS}"})
            return
        results = [{"ref": a["ref"], "answer": a["answer"], "ok": a["ref"] not in failing,
                    **({"error": "demo#139: the clear did not land"} if a["ref"] in failing else {})}
                   for a in answers]
        route.fulfill(json={"results": results, "owner_checks": payload["owner_checks"]})

    authed_page.route(re.compile(r".*/api/board/owner-checks/answer$"), _answer)
    _open_board(authed_page, base_url)
    authed_page.locator("#boardColYours .board-owner-answer-group").click()
    dialog = authed_page.locator("#chiefAnswersDialog")
    blocks = dialog.locator(".owner-answer")
    expect(blocks).to_have_count(40)

    typed = {"demo#101": "validated", "demo#102": "Works fine now.", "demo#104": "ok",
             "demo#105": "the button does nothing", "demo#107": "not working"}
    expected = {}
    # Same `since` everywhere, so the sheet lists the higher issue number first.
    for i, it in enumerate(reversed(items)):
        block = blocks.nth(i)
        ref = it["ref"]
        if ref in typed:
            block.locator(".tr-ask-input").fill(typed[ref])
            continue
        block.locator('.tr-ask-opt[data-answer="validated"]').click()
        expected[ref] = {"ref": ref, "kind": it["kind"], "answer": "validated"}
    expected["demo#101"] = {"ref": "demo#101", "kind": "validate-after", "answer": "validated", "text": "validated"}
    expected["demo#102"] = {"ref": "demo#102", "kind": "validate-after", "answer": "validated",
                            "text": "Works fine now."}
    expected["demo#104"] = {"ref": "demo#104", "kind": "validate-after", "answer": "validated", "text": "ok"}
    expected["demo#105"] = {"ref": "demo#105", "kind": "validate-after", "answer": "problem",
                            "text": "the button does nothing"}
    expected["demo#107"] = {"ref": "demo#107", "kind": "validate-after", "answer": "problem", "text": "not working"}
    # The field says what its text will send, before Done.
    expect(dialog.locator('[data-key="owner:demo#101"] .owner-answer-meaning')).to_contain_text("Validated")
    expect(dialog.locator('[data-key="owner:demo#105"] .owner-answer-meaning')).to_contain_text("problem")

    done = dialog.locator("#chiefAnswersDone")
    expect(done).to_be_enabled()
    done.click()
    failed_block = dialog.locator('[data-key="owner:demo#139"]')
    expect(failed_block.locator(".owner-answer-error")).to_contain_text("did not land")
    expect(dialog).to_be_visible()
    assert all(len(p) <= _MAX_ANSWERS for p in posts), [len(p) for p in posts]
    sent = {a["ref"]: a for p in posts for a in p}
    assert sent == expected, sent
    assert len(chief_posts) == 1, chief_posts
    message = chief_posts[0]["data"]
    assert "[demo#102] Validated: Synthetic check 2 (note: Works fine now.)" in message, message
    assert "[demo#105] Problem found validating Synthetic check 5: the button does nothing" in message, message
    assert "demo#139" not in message, message

    # Done again carries only the answer that did not land.
    failing.clear()
    posts.clear()
    expect(done).to_be_enabled()
    done.click()
    expect(dialog).to_be_hidden()
    assert posts == [[expected["demo#139"]]], posts


@pytest.mark.iphone
def test_owner_checks_use_the_chief_answer_form_at_phone_width(
    authed_page: Page, base_url: str
) -> None:
    """A before-start step and a step to validate are answered on the form a
    chief question has: the option rows of their kind, an Other field with its
    mic and paperclip, and Anything else under them. Each choice sends its own
    answer, a note rides with it, and a Problem found waits for its description."""
    authed_page.set_viewport_size({"width": 390, "height": 844})
    authed_page.add_init_script(_RECORDER_MOCK)
    _route_voice(authed_page, True)
    _mock_board(authed_page, _payload())
    _mock_ensure(authed_page, {})
    chief_posts = _capture_chief_input(authed_page)
    answers: list = []

    def _answer(route):
        answers.append(route.request.post_data_json["answers"])
        results = [{"ref": a["ref"], "answer": a["answer"], "ok": True}
                   for a in route.request.post_data_json["answers"]]
        route.fulfill(json={"results": results, "owner_checks": _payload()["owner_checks"]})

    authed_page.route(re.compile(r".*/api/board/owner-checks/answer$"), _answer)
    _open_board(authed_page, base_url)
    # The sheet reads dictation availability when it opens.
    authed_page.evaluate("async () => { window.__e2eVoice = await import('/static/voice.js'); }")
    authed_page.wait_for_function(
        "() => window.__e2eVoice.voiceDictationAvailable()", timeout=15_000)

    owner = authed_page.locator("#boardColYours .board-owner")
    dialog = authed_page.locator("#chiefAnswersDialog")
    done = dialog.locator("#chiefAnswersDone")

    owner.locator('.board-owner-group[data-group="before_start"] .board-owner-answer-group').click()
    step = dialog.locator('[data-key="owner:demo#7"]')
    labels = step.locator(".tr-ask-opt .tr-ask-label")
    expect(labels).to_have_text(["Done", "Pending", "Pause", "Close (won't do)"])
    expect(step.locator(".tr-ask-other .tr-ask-input")).to_have_count(1)
    expect(step.locator(".tr-ask-other .tr-ask-mic")).to_have_count(1)
    expect(step.locator(".tr-ask-other .tr-ask-attach")).to_have_count(1)
    expect(dialog.locator("#chiefAnswersAlso")).to_be_visible()
    overflow = stable_eval(dialog, "el => el.scrollWidth - el.clientWidth")
    assert overflow <= 0, f"the sheet scrolls sideways by {overflow}px"

    step.locator('.tr-ask-opt[data-answer="pause"]').click()
    step.locator(".tr-ask-input").fill("after the move")
    expect(step.locator(".owner-answer-meaning")).to_have_text("Sends Pause, with this as a note.")
    dialog.locator("#chiefAnswersAlso .composer-input").fill("thanks")
    done.click()
    expect(dialog).to_be_hidden()
    assert answers == [[{"ref": "demo#7", "kind": "before-start", "answer": "pause", "text": "after the move"}]]
    assert chief_posts == [{"data": (
        "Owner checks answered on the Board: Before start\n"
        "[demo#7] Before-start: pause - Pair the kitchen speaker (note: after the move)\n"
        "Also: thanks"
    ), "submit": True}], chief_posts

    answers.clear()
    chief_posts.clear()
    owner.locator('.board-owner-group[data-group="validate_to_close"] .board-owner-answer-group').click()
    check = dialog.locator('[data-key="owner:demo#9"]')
    expect(check.locator(".tr-ask-opt .tr-ask-label")).to_have_text(["Validated", "Problem found"])
    expect(check.locator(".tr-ask-other .tr-ask-mic")).to_have_count(1)
    # Problem found alone is no answer yet: it needs its description.
    check.locator('.tr-ask-opt[data-answer="problem"]').click()
    expect(check.locator(".owner-answer-meaning")).to_have_text("Problem found needs a description in Other.")
    expect(done).to_be_disabled()
    check.locator(".tr-ask-input").fill("it stops after a minute")
    expect(done).to_be_enabled()
    done.click()
    expect(dialog).to_be_hidden()
    assert answers == [[{"ref": "demo#9", "kind": "validate-after", "answer": "problem",
                         "text": "it stops after a minute"}]]
    assert chief_posts == [{"data": (
        "Owner checks answered on the Board: Validate to close\n"
        "[demo#9] Problem found validating Read-aloud on the lock screen: it stops after a minute"
        " (please file a fix)"
    ), "submit": True}], chief_posts
