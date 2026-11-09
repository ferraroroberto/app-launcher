"""The Board tab's badge in the nav (issue #1436, decision 2 of #1432), from
the vendored nav's ``setBadge`` (project-scaffolding#338).

The badge counts what waits on you, visible from every tab: the sessions in
Your turn, plus the chief's unanswered questions since #1502. Off the Board,
the sessions poll (which runs on every tab) carries each session's Board
column (#1434) and the question count (``chief_questions``), so the count
stays live without polling ``/api/board``; on the Board, its own payload is
the count, so the badge matches the lanes. A question count that could not
be read adds nothing. No badge at 0, and the tab's accessible name carries
the count ("Board, 2 waiting").

Hermetic: the session list and the Board are route-mocked before ``goto()``
(#510), with fictional sessions.
"""

from __future__ import annotations

import copy
import json as _json
import re
import time

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import _fake_clock
from tests.e2e.board._chief_support import _plan_with
from tests.e2e.board._support import _board_payload, _mock_board

pytestmark = pytest.mark.smoke

_NOW = int(time.time())


def _session(sid: str, column, status, **extra) -> dict:
    s = {"session_id": sid, "kind": "pty", "agent": "claude", "label": "",
         "project_dir": "E:/work/" + sid, "name": sid, "flags": "",
         "started_at": _NOW - 600, "alive": True, "rows": 40, "cols": 120,
         "live_title": "", "prompt_title": sid + " title", "manual_title": "",
         "output_chars": 10, "board_column": column, "board_status": status}
    s.update(extra)
    return s


_SESSIONS = [
    _session("s-chief", "claude_turn", "awaiting-input", label="chief", name="chief"),
    _session("s-wait", "your_turn", "awaiting-decision"),
    _session("s-stall", "your_turn", "stalled"),
    _session("s-work", "claude_turn", "working"),
    # A Telegram channel session leaves the lists (#1384), and the badge.
    _session("s-tg", "your_turn", "awaiting-input", label="telegram:health"),
]


def _mock_sessions(page: Page, sessions: list[dict], **extra) -> None:
    page.route(re.compile(r".*/api/claude-code/sessions$"), lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=_json.dumps({"sessions": sessions, **extra})))


def _route_board(page: Page, current: dict) -> None:
    """Serve ``current["body"]`` from /api/board at request time."""
    page.route(re.compile(r".*/api/board(?:\?.*)?$"), lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=_json.dumps(current["body"])))


@pytest.mark.iphone
def test_board_badge_counts_your_turn_from_every_tab(
    authed_page: Page, base_url: str
) -> None:
    _mock_board(authed_page)
    current = {"body": _board_payload()}
    _route_board(authed_page, current)
    _mock_sessions(authed_page, _SESSIONS)
    _fake_clock.install(authed_page)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    tab = authed_page.locator("#tabBoard")
    badge = tab.locator(".tab-badge")
    # Code is the first-launch tab: the Board has never been fetched, so the
    # sessions poll is the source — two in Your turn, the channel one not.
    expect(authed_page.locator("#paneClaude")).to_be_visible()
    expect(badge).to_have_text("2")
    expect(badge).to_be_visible()
    expect(tab).to_have_accessible_name("Board, 2 waiting")
    # The badge sits on the icon's corner: inside the tab sideways, and inside
    # the bar vertically (the nav's icon sits high in the pill, so the badge
    # may overhang the tab's own top edge by a pixel or two).
    tab_box = tab.bounding_box()
    badge_box = badge.bounding_box()
    bar_box = authed_page.locator("nav.tabs").bounding_box()
    assert tab_box and badge_box and bar_box, "nav badge not laid out"
    assert tab_box["x"] <= badge_box["x"] and badge_box["x"] + badge_box["width"] <= tab_box["x"] + tab_box["width"] + 1
    assert bar_box["y"] <= badge_box["y"], (bar_box, badge_box)

    # On the Board its own payload is the count: one session in Your turn.
    tab.click()
    expect(authed_page.locator('.board-list[data-col="your_turn"] li.board-item')).to_have_count(1)
    expect(badge).to_have_text("1")
    expect(tab).to_have_accessible_name("Board, 1 waiting")

    # Nothing waiting: no badge at all, and the name is the plain label.
    empty = copy.deepcopy(current["body"])
    empty["columns"]["your_turn"] = []
    current["body"] = empty
    # One Board poll, jumped on the fake clock (#1515) rather than waited out.
    with authed_page.expect_response(lambda r: r.url.endswith("/api/board")):
        _fake_clock.jump_interval(authed_page, 5_000, "fetchBoard")
    expect(badge).to_have_count(0)
    expect(tab).to_have_accessible_name("Board")


@pytest.mark.iphone
def test_board_badge_and_header_count_the_chiefs_open_questions(
    authed_page: Page, base_url: str, tmp_path
) -> None:
    """#1502 (decision 1 of #1500): the chief's unanswered questions,
    fleet-wide, join Your turn in the badge from every tab and lead the
    Board's header ("2 questions · 1 needs you"). An answered question is not
    counted."""
    plan = _plan_with(tmp_path, [
        {"id": "q-a", "text": "Ship the widget?", "ref": "demo-repo#1"},
        {"text": "Rename the gadget?", "ref": "other-repo#2"},
        {"text": "Keep the sprocket?"},
    ])
    plan["answered"] = ["q-a"]
    _mock_board(authed_page, _board_payload(), plan=plan)
    # Off the Board the webapp's own count rides the sessions poll.
    _mock_sessions(authed_page, _SESSIONS, chief_questions=5)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    tab = authed_page.locator("#tabBoard")
    badge = tab.locator(".tab-badge")
    expect(authed_page.locator("#paneClaude")).to_be_visible()
    expect(badge).to_have_text("7")
    expect(tab).to_have_accessible_name("Board, 7 waiting")

    # On the Board its own payload is both halves: 1 in Your turn, 2 open.
    tab.click()
    expect(authed_page.locator("#boardChiefPlan li.board-plan-waiting")).to_have_count(3)
    expect(badge).to_have_text("3")
    expect(tab).to_have_accessible_name("Board, 3 waiting")
    head = authed_page.locator("#boardHeadStatus")
    # Three exceptions, two shown, worst tone first (#1586): the failing job
    # (danger) leads and pushes "needs you" out; the questions keep their
    # place ahead of it.
    expect(head).to_have_text("1 failing · 2 questions")
    parts = head.locator(".head-exception")
    expect(parts).to_have_count(2)
    expect(parts.nth(0)).to_have_attribute("data-tone", "danger")
    expect(parts.nth(1)).to_have_attribute("data-tone", "attention")
    # Two parts on one line at the phone's width, no word clipped.
    clipped = authed_page.evaluate(
        "() => { const el = document.getElementById('boardHeadStatus');"
        " return el.scrollWidth > el.clientWidth + 1; }")
    assert not clipped, "the Board header clips its two parts"


def _owner_section(marks: list) -> dict:
    """An available owner-check list whose items carry ``marks`` (None = waiting)."""
    return {
        "available": True, "fetched_at": "2026-11-02T08:00:00Z", "error": None,
        "refreshing": False, "unknown": [],
        "items": [
            {"ref": f"demo#{i}", "repo": "demo", "number": i, "title": f"Check {i}",
             "url": f"https://github.com/octo/demo/issues/{i}", "state": "OPEN",
             "kind": "before-start" if i % 2 else "validate-after", "block": False,
             "reason": "", "try": "", "proof": [], "since": "2026-11-01",
             "mark": mark, "mark_at": None, "mark_until": None}
            for i, mark in enumerate(marks, start=1)
        ],
    }


@pytest.mark.iphone
def test_board_badge_counts_owner_checks_off_the_board_and_icon_stays_24px(
    authed_page: Page, base_url: str
) -> None:
    """#2059: the owner checks that wait on the owner join the badge from every
    tab, via the sessions poll's ``owner_checks``; a count the server could not
    read adds nothing. The Board icon keeps its 24px box with the badge on it:
    the badge overhangs the icon's box by design, it does not resize it."""
    _mock_board(authed_page)
    current = {"owner_checks": 4, "sessions": _SESSIONS}

    def serve(route) -> None:
        route.fulfill(status=200, content_type="application/json", body=_json.dumps(
            {"sessions": current["sessions"], "chief_questions": 1,
             "owner_checks": current["owner_checks"]}))

    authed_page.route(re.compile(r".*/api/claude-code/sessions$"), serve)
    _fake_clock.install(authed_page)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    tab = authed_page.locator("#tabBoard")
    badge = tab.locator(".tab-badge")
    expect(authed_page.locator("#paneClaude")).to_be_visible()
    # Two sessions in Your turn, one chief question, four owner checks.
    expect(badge).to_have_text("7")
    expect(tab).to_have_accessible_name("Board, 7 waiting")
    icon = tab.locator(".tab-icon").bounding_box()
    assert icon and round(icon["width"]) == 24 and round(icon["height"]) == 24, icon

    # The next poll cannot read the count: it adds nothing, never a zero.
    current["owner_checks"] = None
    with authed_page.expect_response(lambda r: r.url.endswith("/api/claude-code/sessions")):
        _fake_clock.jump_interval(authed_page, 5_000, "fetchSessions")
    expect(badge).to_have_text("3")
    expect(tab).to_have_accessible_name("Board, 3 waiting")

    # Past nine the badge caps at "9+" and the accessible name keeps the real count.
    current["sessions"] = [_session(f"s-{i}", "your_turn", "awaiting-input") for i in range(12)]
    with authed_page.expect_response(lambda r: r.url.endswith("/api/claude-code/sessions")):
        _fake_clock.jump_interval(authed_page, 5_000, "fetchSessions")
    expect(badge).to_have_text("9+")
    expect(tab).to_have_accessible_name("Board, 13 waiting")


def test_board_badge_on_the_board_adds_owner_checks_that_still_wait(
    authed_page: Page, base_url: str
) -> None:
    """#2059: on the Board its own payload is every part, the same sum as the
    head line's "needs you": Your turn plus the unmarked owner checks, an item
    answered Later or Problem left out. #1502: a plan that can't be read is
    unknown, never zero and never a guess: the header shows no question part
    and the badge counts no question, on the Board and off it."""
    payload = _board_payload()
    payload["owner_checks"] = _owner_section([None, None, "later", "problem"])
    _mock_board(authed_page, payload, plan={"state": "unreadable"})
    _mock_sessions(authed_page, _SESSIONS, chief_questions=None, owner_checks=None)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    tab = authed_page.locator("#tabBoard")
    badge = tab.locator(".tab-badge")
    expect(authed_page.locator("#paneClaude")).to_be_visible()
    expect(badge).to_have_text("2")
    tab.click()
    expect(authed_page.locator("#boardChiefPlan .board-plan-note")).to_have_text("Plan unreadable.")
    expect(authed_page.locator("#boardHeadStatus")).to_have_text("1 failing · 3 needs you")
    expect(badge).to_have_text("3")
    expect(tab).to_have_accessible_name("Board, 3 waiting")
