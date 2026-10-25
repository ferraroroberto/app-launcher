"""The chief's plan: model chips, the Answer button, the repo filter, shared answered marks and its row layout."""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from src import chief_plan
from tests.e2e import _fake_clock
from tests.e2e.conftest import (
    _seed_token_init_script, stable_eval, stable_read, wait_until,
)

from tests.e2e.board._chief_support import (
    _QUESTIONS,
    _SharedPlanServer,
    _board_payload,
    _capture_chief_input,
    _mock_board,
    _mock_ensure,
    _open_board,
    _plan_with,
    _route_plan_from,
)


pytestmark = pytest.mark.smoke


def test_plan_rows_show_the_model_only_when_the_chief_named_one(
    authed_page: Page, base_url: str, tmp_path
) -> None:
    """#1352: a lane or queue row that carries a `model` shows it as quiet
    text before its status chip; a row without one (or with a non-string one)
    shows nothing extra, and keeps its status chip and title."""
    _mock_board(authed_page, _board_payload(with_chief=True))
    f = tmp_path / "chief-plan.json"
    f.write_text(_json.dumps({
        "version": 1, "updated_at": "2026-09-27T09:00:00Z",
        "lanes": [
            {"repo": "app-launcher", "session": "s-a", "item": "#1352", "status": "building",
             "model": "opus"},
            {"repo": "fleet-config", "session": "s-f", "item": "#959", "status": "gate"},
        ],
        "queue": [
            {"repo": "app-launcher", "ref": "#1199", "title": "pin the model", "status": "queued",
             "model": "sonnet"},
            {"repo": "app-launcher", "ref": "#1354", "title": "photo batch", "status": "queued",
             "model": 5},
        ],
    }), encoding="utf-8")
    _route_plan_from(authed_page, {"plan": chief_plan.read_chief_plan(f, "octo")})
    _open_board(authed_page, base_url)

    plan_body = authed_page.locator("#boardChiefPlan .board-plan-body")
    expect(plan_body).to_have_attribute("data-state", "ok")
    lanes = plan_body.locator(".board-plan-group").nth(0).locator("li")
    queue = plan_body.locator(".board-plan-group").nth(1).locator("li")
    expect(lanes).to_have_count(2)
    expect(queue).to_have_count(2)

    expect(lanes.nth(0).locator(".board-plan-model")).to_have_text("Opus")
    expect(lanes.nth(0).locator(".board-plan-model")).to_be_visible()
    expect(lanes.nth(0).locator(".board-plan-chip")).to_have_text("building")
    expect(queue.nth(0).locator(".board-plan-model")).to_have_text("Sonnet")
    expect(queue.nth(0).locator(".board-plan-model")).to_be_visible()
    expect(queue.nth(0).locator(".board-plan-chip")).to_have_text("queued")

    # No model (missing, or the wrong type): today's row, no extra text.
    expect(lanes.nth(1).locator(".board-plan-model")).to_have_count(0)
    expect(lanes.nth(1).locator(".board-plan-chip")).to_have_text("gate")
    expect(queue.nth(1).locator(".board-plan-model")).to_have_count(0)
    expect(queue.nth(1)).to_contain_text("photo batch")
    expect(queue.nth(1).locator(".board-plan-chip")).to_have_text("queued")


@pytest.mark.iphone
def test_answer_button_shows_only_with_questions(
    authed_page: Page, base_url: str, tmp_path
) -> None:
    """#1295: the Chief's-plan card offers "Answer N questions" only when
    something waits on Roberto; with nothing waiting there is no button."""
    _mock_board(authed_page, _board_payload(with_chief=True))
    current = {"plan": _plan_with(tmp_path, [])}
    _route_plan_from(authed_page, current)
    _open_board(authed_page, base_url)
    plan_body = authed_page.locator("#boardChiefPlan .board-plan-body")
    expect(plan_body).to_have_attribute("data-state", "ok")
    expect(plan_body.locator(".board-plan-answer")).to_have_count(0)

    current["plan"] = _plan_with(tmp_path, _QUESTIONS)
    authed_page.locator("#boardRefresh").click()
    button = plan_body.locator(".board-plan-answer")
    expect(button).to_have_text("Answer 3 questions")
    # The card lists every item, an old text-only one included.
    expect(plan_body.locator("li.board-plan-waiting")).to_have_count(3)
    expect(plan_body.locator("li.board-plan-waiting").nth(2)).to_contain_text("Remember the last tab?")
    box = stable_read(button.bounding_box)
    assert box and box["height"] >= 44, f"answer button under the 44px tap floor: {box}"


def _mock_repo_filter_board(page: Page, tmp_path) -> list:
    """Mock the apps, board, plan, ensure and input routes for the repo-filter test; return the input posts."""
    page.route(
        re.compile(r".*/api/apps$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"scan_root": "", "apps": [
                {"id": "cc-" + name, "kind": "claude-code", "name": name,
                 "project_dir": "E:/automation/" + name}
                for name in ("app-launcher", "fleet-config", "life-os")
            ]}),
        ),
    )
    _mock_board(page, _board_payload(with_chief=True))
    f = tmp_path / "chief-plan.json"
    f.write_text(_json.dumps({
        "version": 1, "updated_at": "2026-09-27T09:00:00Z",
        "lanes": [
            {"repo": "app-launcher", "session": "s-a", "item": "#1295", "status": "building"},
            {"repo": "fleet-config", "session": "s-f", "item": "#959", "status": "gate"},
        ],
        "queue": [
            {"repo": "app-launcher", "ref": "#1295", "title": "answer sheet", "status": "building"},
            {"ref": "fleet-config#900", "title": "hooks sweep", "status": "someday"},
        ],
        # fleet-config (repo + ref), life-os (repo only), app-launcher (ref
        # only), then one with no repo at all.
        "waiting_on_roberto": _QUESTIONS + [
            {"id": "q-pause", "text": "Pause every lane tonight?"},
        ],
    }), encoding="utf-8")
    _route_plan_from(page, {"plan": chief_plan.read_chief_plan(f, "octo")})
    ensured: dict = {}
    _mock_ensure(page, ensured)
    posts = _capture_chief_input(page)
    return posts


def test_repo_filter_narrows_the_plan_and_its_questions(
    authed_page: Page, base_url: str, tmp_path
) -> None:
    """#1332: the Dispatch repo picker narrows the Chief's plan as it does
    the columns. Waiting on you, Lanes and Queue keep only the picked repo's
    rows (a repo from `repo`, or the repo half of a `repo#N` ref) plus rows
    with no repo, which are fleet-wide and never hidden; a note counts the
    questions the filter hides; a group the filter empties is gone. "Answer
    N questions" counts and opens only what shows, the message numbers each
    by its place in the whole plan and doesn't list hidden ones as skipped,
    and clearing the filter restores everything."""
    posts = _mock_repo_filter_board(authed_page, tmp_path)
    _open_board(authed_page, base_url)

    plan_body = authed_page.locator("#boardChiefPlan .board-plan-body")
    waiting = plan_body.locator("li.board-plan-waiting")
    headings = plan_body.locator(".board-plan-heading")
    button = plan_body.locator(".board-plan-answer")
    hidden_note = plan_body.locator(".board-plan-hidden")
    expect(waiting).to_have_count(4)
    expect(button).to_have_text("Answer 4 questions")
    expect(hidden_note).to_have_count(0)

    def pick(repo: str) -> None:
        authed_page.locator("#boardDispatchRepoBtn").click()
        item = authed_page.locator(f'#boardDispatchRepoList li[data-repo="{repo}"]')
        expect(item).to_be_visible(timeout=15_000)
        item.click()

    pick("app-launcher")
    expect(waiting).to_have_count(2)
    expect(waiting.nth(0)).to_contain_text("Remember the last tab?")
    expect(waiting.nth(1)).to_contain_text("Pause every lane tonight?")
    expect(button).to_have_text("Answer 2 questions")
    expect(hidden_note).to_have_text("2 more questions in other projects")
    expect(headings).to_have_text(["Waiting on you", "Lanes (1)", "Queue (1)"])
    expect(plan_body.locator(".board-plan-group").nth(1).locator("li")).to_have_count(1)
    expect(plan_body.locator(".board-plan-group").nth(2).locator("li")).to_have_count(1)
    expect(plan_body.locator(".board-plan-group").nth(2)).to_contain_text("answer sheet")

    # No lane or queue row is life-os's, so both groups go, not left empty.
    pick("life-os")
    expect(waiting).to_have_count(2)
    expect(waiting.nth(0)).to_contain_text("Which days?")
    expect(headings).to_have_text(["Waiting on you"])
    expect(hidden_note).to_have_text("2 more questions in other projects")

    # The sheet asks only what shows, each numbered by its place in the plan.
    pick("app-launcher")
    button.click()
    dialog = authed_page.locator("#chiefAnswersDialog")
    blocks = dialog.locator(".chief-answer")
    expect(blocks).to_have_count(2)
    expect(blocks.nth(0).locator(".chief-answer-num")).to_have_text("3")
    expect(blocks.nth(1).locator(".chief-answer-num")).to_have_text("4")
    blocks.nth(0).locator(".tr-ask-input").fill("yes")
    dialog.locator("#chiefAnswersDone").click()
    expect(dialog).to_be_hidden()
    assert posts == [{
        "data": (
            "Answers from the Board (1 of 2):\n"
            "3. [app-launcher#1131] Remember the last tab? → Other: yes\n"
            "Skipped: 4"
        ),
        "submit": True,
    }], posts

    # All projects again: the whole plan, and the answered mark on its row;
    # the button no longer counts the answered question (#1487).
    pick("")
    expect(waiting).to_have_count(4)
    expect(button).to_have_text("Answer 3 questions")
    expect(hidden_note).to_have_count(0)
    expect(headings).to_have_text(["Waiting on you", "Lanes (2)", "Queue (2)"])
    expect(waiting.nth(2)).to_contain_text("answered, waiting for the chief")
    expect(waiting.nth(0)).not_to_contain_text("answered")


def test_answered_marks_show_on_every_device_until_the_plan_moves(
    authed_page: Page, browser, browser_context_args, auth_token: str,
    base_url: str, tmp_path,
) -> None:
    """#1487: what one device answers reads "answered, waiting for the
    chief" on another — polling or reloaded — and neither counts nor asks it
    again. The marks clear on both when the chief rewrites the plan. Page B
    is its own browser context (its own storage), so nothing local can carry
    the marks across; the chief's input is the test's capture, never a real
    PTY."""
    server = _SharedPlanServer(tmp_path, _QUESTIONS)
    context_b = browser.new_context(**browser_context_args)
    if auth_token:
        context_b.add_init_script(_seed_token_init_script(auth_token))
    page_b = context_b.new_page()
    try:
        pages = (authed_page, page_b)
        for page in pages:
            _mock_board(page, _board_payload(with_chief=True))
            server.route(page)
        _mock_ensure(authed_page, {})
        posts = _capture_chief_input(authed_page)
        posts_b = _capture_chief_input(page_b)
        for page in pages:
            _open_board(page, base_url)
            expect(page.locator("#boardChiefPlan .board-plan-answer")).to_have_text("Answer 3 questions")

        # Device A answers the first and third questions.
        authed_page.locator("#boardChiefPlan .board-plan-answer").click()
        blocks = authed_page.locator("#chiefAnswersDialog .chief-answer")
        expect(blocks).to_have_count(3)
        blocks.nth(0).locator(".tr-ask-opt").nth(0).click()
        blocks.nth(2).locator(".tr-ask-input").fill("yes")
        authed_page.locator("#chiefAnswersDone").click()
        expect(authed_page.locator("#chiefAnswersDialog")).to_be_hidden()
        wait_until(authed_page, lambda: len(server.records) == 1, "the answered marks recorded")
        assert len(posts) == 1 and server.records == [{
            "updated_at": "2026-09-27T09:00:00Z", "ids": ["q-plans", "2"],
        }], server.records

        def expect_answered(page: Page, marked: list, button: str) -> None:
            rows = page.locator("#boardChiefPlan li.board-plan-waiting")
            expect(rows).to_have_count(3)
            for i, on in enumerate(marked):
                if on:
                    expect(rows.nth(i)).to_contain_text("answered, waiting for the chief")
                    expect(rows.nth(i)).to_have_class(re.compile(r"\bis-answered\b"))
                else:
                    expect(rows.nth(i)).not_to_contain_text("answered")
            expect(page.locator("#boardChiefPlan .board-plan-answer")).to_have_text(button)

        expect_answered(authed_page, [True, False, True], "Answer 1 question")
        # Device B on its next poll (↻ runs the poll's fetch), then reloaded.
        page_b.locator("#boardRefresh").click()
        expect_answered(page_b, [True, False, True], "Answer 1 question")
        _open_board(page_b, base_url)
        expect_answered(page_b, [True, False, True], "Answer 1 question")
        # B's sheet asks only what is still open, numbered by its plan place.
        page_b.locator("#boardChiefPlan .board-plan-answer").click()
        sheet_b = page_b.locator("#chiefAnswersDialog .chief-answer")
        expect(sheet_b).to_have_count(1)
        expect(sheet_b.locator(".chief-answer-num")).to_have_text("2")
        page_b.locator("#chiefAnswersClose").click()
        assert posts_b == [], "device B sent nothing"

        # The chief rewrites the plan: both devices show every question open.
        server.rewrite("2026-09-27T09:10:00Z")
        for page in pages:
            page.locator("#boardRefresh").click()
            expect_answered(page, [False, False, False], "Answer 3 questions")
    finally:
        context_b.close()


@pytest.mark.iphone
def test_questions_are_one_line_rows_three_then_more_then_one_answer(
    authed_page: Page, base_url: str, tmp_path
) -> None:
    """#1502 (decision 2 of #1500): each waiting question is one shared
    action-row, its text on one line with the ref as meta and a chevron, no
    amber bar. The first three show, then one "N more" row that unfolds the
    rest in place, then the one Answer button. A tap on a row opens the
    answer sheet on that question."""
    waiting = [
        {"id": f"q-{i}", "ref": f"demo-repo#{i}",
         "text": f"Question {i}: should the sprocket keep a deliberately long name "
                 "that no row on a phone, nor a lane on the widest desktop grid, "
                 "can ever hold on one line without cutting it short?",
         "question": f"Full question {i}?"}
        for i in range(1, 7)
    ]
    _mock_board(authed_page, _board_payload(with_chief=True))
    _route_plan_from(authed_page, {"plan": _plan_with(tmp_path, waiting)})
    _fake_clock.install(authed_page)
    _open_board(authed_page, base_url)

    group = authed_page.locator("#boardChiefPlan .board-plan-group-waiting")
    rows = group.locator("li.board-plan-waiting")
    more = group.locator("li.board-plan-more")
    expect(rows).to_have_count(3)
    expect(more).to_have_count(1)
    expect(more.locator(".action-row-title")).to_have_text("3 more questions")
    expect(group.locator(".board-plan-answer")).to_have_text("Answer 6 questions")
    # Order: the rows, then "N more", then the button.
    expect(group.locator("li")).to_have_count(4)
    expect(group.locator("li").nth(3)).to_have_class(re.compile(r"\bboard-plan-more\b"))

    first = rows.nth(0)
    expect(first).to_have_class(re.compile(r"\baction-row\b"))
    expect(first.locator(".action-row-meta")).to_have_text("demo-repo#1")
    expect(first.locator(".board-plan-chevron")).to_have_count(1)
    # No amber bar or stripe left on the row.
    expect(first).to_have_css("box-shadow", "none")
    # One line at the rows.md height, the text clipped by its ellipsis.
    m = stable_eval(first, (
        "el => { const t = el.querySelector('.action-row-title');"
        " if (!t || !t.clientWidth) return null;"
        " return {h: el.getBoundingClientRect().height,"
        " clipped: t.scrollWidth > t.clientWidth,"
        " th: t.getBoundingClientRect().height,"
        " lh: parseFloat(getComputedStyle(t).lineHeight)}; }"))
    assert m is not None
    assert abs(m["h"] - 52) <= 1, f"question row is not rows.md tall: {m}"
    assert m["th"] <= m["lh"] + 1, f"question title wraps: {m}"
    assert m["clipped"], f"a long question should ellipsize: {m}"

    # "N more" unfolds the rest in place, and stays unfolded across a poll.
    more.locator(".action-row-main").click()
    expect(rows).to_have_count(6)
    expect(more).to_have_count(0)
    with authed_page.expect_response(
        lambda resp: resp.url.endswith("/api/board/chief-plan")
    ):
        _fake_clock.jump_interval(authed_page, 5_000, "fetchBoard")
    authed_page.evaluate(
        "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
    expect(rows).to_have_count(6)

    # A row opens the sheet on its own question: the last one, scrolled to.
    rows.nth(5).locator(".action-row-main").click()
    dialog = authed_page.locator("#chiefAnswersDialog")
    expect(dialog).to_be_visible()
    expect(dialog.locator(".chief-answer")).to_have_count(6)
    expect(dialog.locator('.chief-answer[data-key="q-6"] .tr-ask-question')).to_be_in_viewport()
    expect(dialog.locator('.chief-answer[data-key="q-6"] .tr-ask-question')).to_have_text(
        "Full question 6?")


@pytest.mark.iphone
def test_plan_sections_split_queue_from_parked_and_rows_open_their_issue(
    authed_page: Page, base_url: str, tmp_path
) -> None:
    """#1578: Lanes, Queue and Parked each carry a count; a `parked` or
    `waiting-roberto` item leaves the Queue for Parked, which starts folded on
    a phone. A row is two lines (title, then `repo#N · Model` with the status
    chip at that line's right end) and the title is readable on its own line;
    a row with a `repo#N` ref opens its issue on a tap of the row or the
    chevron, a row without one stays inert."""
    authed_page.add_init_script(
        "window.__opened = [];"
        " window.open = function (u) { window.__opened.push(u); return null; };")
    _mock_board(authed_page, _board_payload(with_chief=True))
    f = tmp_path / "chief-plan.json"
    f.write_text(_json.dumps({
        "version": 1, "updated_at": "2026-09-27T09:00:00Z",
        "lanes": [
            {"repo": "app-launcher", "session": "s-a", "item": "#1578", "status": "building",
             "model": "opus"},
        ],
        "queue": [
            {"repo": "app-launcher", "ref": "#1500", "title": "Adopt the usage log",
             "status": "queued", "model": "sonnet"},
            {"repo": "fleet-config", "ref": "#12", "title": "Second in line", "status": "building"},
            {"repo": "app-launcher", "ref": "#1400", "title": "Parked with a ref", "status": "parked"},
            {"repo": "automation", "ref": "", "title": "Parked without a ref",
             "status": "waiting-roberto"},
        ],
    }), encoding="utf-8")
    _route_plan_from(authed_page, {"plan": chief_plan.read_chief_plan(f, "octo")})
    _open_board(authed_page, base_url)

    plan_body = authed_page.locator("#boardChiefPlan .board-plan-body")
    expect(plan_body).to_have_attribute("data-state", "ok")
    expect(plan_body.locator(".board-plan-heading")).to_have_text(
        ["Lanes (1)", "Queue (2)", "Parked (2)"])
    queue_group = plan_body.locator(".board-plan-group").nth(1)
    queue = queue_group.locator("li.board-plan-row")
    expect(queue).to_have_count(2)
    expect(queue.locator(".action-row-title")).to_have_text(["Adopt the usage log", "Second in line"])
    expect(queue.locator(".board-plan-chip")).to_have_text(["queued", "building"])
    expect(queue_group.locator(".board-plan-section-note")).to_contain_text("Backlog")

    # Parked folds on a phone, opens on the desktop board.
    parked = plan_body.locator(".board-plan-parked")
    parked_rows = parked.locator("li.board-plan-row")
    on_phone = (authed_page.viewport_size or {"width": 0})["width"] < 700
    if on_phone:
        expect(parked_rows.first).to_be_hidden()
        summary = parked.locator("summary")
        box = stable_read(summary.bounding_box)
        assert box and box["height"] >= 44, f"Parked summary is below a 44px target: {box}"
        summary.click()
    expect(parked_rows).to_have_count(2)
    expect(parked_rows.first).to_be_visible()
    expect(parked_rows.locator(".board-plan-chip")).to_have_text(["parked", "waiting-roberto"])

    # Two lines: the title over the meta, the chip at the meta line's right
    # end, the chevron after the title's column, the whole row a 44px target.
    expect(queue.first.locator(".action-row-meta-text")).to_have_text("app-launcher#1500 · Sonnet")
    expect(queue.first.locator(".board-plan-model")).to_have_text("Sonnet")
    geo = stable_eval(queue.first, (
        "el => { const r = el.getBoundingClientRect();"
        " const t = el.querySelector('.action-row-title');"
        " const tb = t.getBoundingClientRect();"
        " const m = el.querySelector('.action-row-meta-text').getBoundingClientRect();"
        " const c = el.querySelector('.board-plan-chip').getBoundingClientRect();"
        " const v = el.querySelector('.board-plan-chevron').getBoundingClientRect();"
        " return {h: r.height, titleBottom: tb.bottom, metaTop: m.top, titleRight: tb.right,"
        " chipLeft: c.left, metaLeft: m.left, chipRight: c.right, chevLeft: v.left,"
        " chevW: v.width, clipped: t.scrollWidth > t.clientWidth}; }"))
    assert geo["h"] >= 44, geo
    assert geo["metaTop"] >= geo["titleBottom"] - 1, f"meta is not under the title: {geo}"
    assert geo["chipLeft"] > geo["metaLeft"] and geo["chipRight"] <= geo["chevLeft"] + 1, geo
    assert geo["titleRight"] <= geo["chevLeft"] + 1, f"title runs under the chevron: {geo}"
    assert geo["chevW"] >= 44, geo
    assert not geo["clipped"], f"a short queue title is clipped: {geo}"

    # Taps: the row, then its chevron, open the issue; the lane opens its own;
    # a parked row with no ref does nothing.
    def opened() -> list:
        return authed_page.evaluate("() => window.__opened")

    queue.first.locator(".action-row-main").click()
    queue.first.locator(".board-plan-chevron").click()
    plan_body.locator(".board-plan-group").nth(0).locator(".action-row-main").click()
    assert opened() == [
        "https://github.com/octo/app-launcher/issues/1500",
        "https://github.com/octo/app-launcher/issues/1500",
        "https://github.com/octo/app-launcher/issues/1578",
    ]
    inert = parked_rows.nth(1)
    expect(inert.locator(".action-row-main")).to_have_class(re.compile(r"is-inert"))
    expect(inert.locator(".board-plan-chevron")).to_have_count(0)
    inert.locator(".action-row-main").click()
    assert len(opened()) == 3
