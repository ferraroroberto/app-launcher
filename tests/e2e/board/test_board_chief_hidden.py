"""The fleet chief leaves the Board's lanes and every waiting count, like a Telegram session.

The chief is a managed session: its conversation lives in the Board chat bar, so an idle
chief card or a "needs you" tally for it is noise. The Coding tab keeps its row. With the
``hide_channel_sessions`` setting off it renders as before. Hermetic: the Board, the
session list and the config are route-mocked, with fictional sessions.
"""

from __future__ import annotations

import json
import re
import time

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.board._chief_support import (
    _board_payload, _mock_board, _open_board, _show_managed_sessions,
)

pytestmark = pytest.mark.smoke

_NOW = int(time.time())


def _session(sid: str, label: str, name: str, column: str, status: str) -> dict:
    return {"session_id": sid, "kind": "pty", "agent": "claude", "label": label,
            "project_dir": "E:/work/" + name, "name": name, "flags": "",
            "started_at": _NOW - 600, "alive": True, "rows": 40, "cols": 120,
            "live_title": "", "prompt_title": name + " title", "manual_title": "",
            "output_chars": 10, "board_column": column, "board_status": status}


# The chief is routed into Claude's turn by the server, but a stale session-host list can
# still hand the client one in Your turn: the filter must not rely on the lane.
_SESSIONS = [
    _session("s-chief", "chief", "chief", "your_turn", "awaiting-input"),
    _session("s-work", "", "life-os", "claude_turn", "working"),
]


def _mock(page: Page, *, hidden: bool) -> None:
    _mock_board(page, _board_payload(with_chief=True))
    page.route(re.compile(r".*/api/claude-code/sessions$"), lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"sessions": _SESSIONS})))
    if hidden:
        # Registered after _mock_board's own route, so this one wins.
        page.route(re.compile(r".*/api/config$"), _hide_managed_sessions)
    else:
        page.route(re.compile(r".*/api/config$"), _show_managed_sessions)


def _hide_managed_sessions(route) -> None:
    """GET /api/config with ``hide_channel_sessions`` on, whatever the box's own file says."""
    resp = route.fetch()
    body = resp.json()
    body["hide_channel_sessions"] = True
    route.fulfill(response=resp, json=body)


def test_chief_is_out_of_the_lanes_and_the_waiting_count(
    authed_page: Page, base_url: str
) -> None:
    _mock(authed_page, hidden=True)
    _open_board(authed_page, base_url)

    expect(authed_page.locator('.board-list[data-col="claude_turn"] li.board-item')
           .filter(has_text="life-os")).to_have_count(1)
    expect(authed_page.locator("li.board-item-chief")).to_have_count(0)
    # One worker: the header counts it, not the chief; nothing waits on the owner.
    expect(authed_page.locator("#boardHeadStatus")).to_contain_text("1 session")
    expect(authed_page.locator("#tabBoard .tab-badge")).to_have_count(0)

    # The Coding tab keeps the chief's row, and it does not claim to need you.
    authed_page.locator("#tabClaude").click()
    row = authed_page.locator('#sessionsList li[data-session-id="s-chief"]')
    expect(row).to_have_count(1)
    expect(row.locator(".session-attention")).to_have_count(0)


def _external_card(name: str, *, job_run: bool) -> dict:
    return {"session_id": None, "kind": "external", "agent": "claude",
            "project_dir": "E:/work/" + name, "name": name, "alive": None,
            "started_at": None, "live_title": "", "prompt_title": "", "manual_title": "",
            "project": name, "status": "awaiting-input", "age_seconds": 120,
            "job_run": job_run}


def test_job_run_session_is_out_of_the_lanes_and_the_waiting_count(
    authed_page: Page, base_url: str
) -> None:
    payload = _board_payload(with_chief=False)
    payload["columns"]["your_turn"] = [
        _external_card("weekly-cleanup", job_run=True),
        _external_card("scratch-repo", job_run=False),
    ]
    _mock_board(authed_page, payload)
    authed_page.route(re.compile(r".*/api/config$"), _hide_managed_sessions)
    _open_board(authed_page, base_url)

    lanes = authed_page.locator("li.board-item")
    expect(lanes.filter(has_text="scratch-repo")).to_have_count(1)
    expect(lanes.filter(has_text="weekly-cleanup")).to_have_count(0)
    # Only the ordinary external card waits on the owner; the job run is not counted.
    expect(authed_page.locator("#boardHeadStatus")).to_contain_text("1 needs you")
    expect(authed_page.locator("#tabBoard .tab-badge")).to_have_text("1")


def test_setting_off_puts_the_chief_back_on_the_board(
    authed_page: Page, base_url: str
) -> None:
    _mock(authed_page, hidden=False)
    _open_board(authed_page, base_url)

    expect(authed_page.locator("li.board-item-chief")).to_have_count(1)
    expect(authed_page.locator("#boardHeadStatus")).to_contain_text("2 sessions")
