"""The Board tab's "Your turn" badge in the nav (issue #1436, decision 2 of
#1432), from the vendored nav's ``setBadge`` (project-scaffolding#338).

The badge counts the sessions in Your turn and is visible from every tab.
Off the Board, the sessions poll (which runs on every tab) carries each
session's Board column (#1434), so the count stays live without polling
``/api/board``; on the Board, its own payload is the count, so the badge
matches the lane. No badge at 0, and the tab's accessible name carries the
count ("Board, 2 waiting").

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

from tests.e2e.test_board_tab import _FAKE_BOARD, _board_payload, _mock_board

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


def _mock_sessions(page: Page, sessions: list[dict]) -> None:
    page.route(re.compile(r".*/api/claude-code/sessions$"), lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=_json.dumps({"sessions": sessions})))


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
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    tab = authed_page.locator("#tabBoard")
    badge = tab.locator(".tab-badge")
    # Code is the first-launch tab: the Board has never been fetched, so the
    # sessions poll is the source — two in Your turn, the channel one not.
    expect(authed_page.locator("#paneClaude")).to_be_visible()
    expect(badge).to_have_text("2")
    expect(badge).to_be_visible()
    expect(tab).to_have_accessible_name("Board, 2 waiting")
    # The badge sits on the icon's corner, inside the tab.
    tab_box = tab.bounding_box()
    badge_box = badge.bounding_box()
    assert tab_box and badge_box, "nav badge not laid out"
    assert tab_box["x"] <= badge_box["x"] and badge_box["x"] + badge_box["width"] <= tab_box["x"] + tab_box["width"] + 1
    assert tab_box["y"] <= badge_box["y"] + 1, (tab_box, badge_box)

    # On the Board its own payload is the count: one session in Your turn.
    tab.click()
    expect(authed_page.locator('.board-list[data-col="your_turn"] li.board-item')).to_have_count(1)
    expect(badge).to_have_text("1")
    expect(tab).to_have_accessible_name("Board, 1 waiting")

    # Nothing waiting: no badge at all, and the name is the plain label.
    empty = copy.deepcopy(current["body"])
    empty["columns"]["your_turn"] = []
    current["body"] = empty
    with authed_page.expect_response(lambda r: r.url.endswith("/api/board"), timeout=15_000):
        pass
    expect(badge).to_have_count(0)
    expect(tab).to_have_accessible_name("Board")


def test_board_badge_caps_at_nine_plus_and_keeps_the_real_count_spoken(
    authed_page: Page, base_url: str
) -> None:
    _mock_board(authed_page, copy.deepcopy(_FAKE_BOARD))
    many = [_session(f"s-{i}", "your_turn", "awaiting-input") for i in range(12)]
    _mock_sessions(authed_page, many)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    tab = authed_page.locator("#tabBoard")
    expect(tab.locator(".tab-badge")).to_have_text("9+")
    expect(tab).to_have_accessible_name("Board, 12 waiting")
