"""The Code tab's session rows carry the Board's status and age (issue #1580).

A row reads "folder · status · age", then "up uptime": the same words, from
the same shared formatter (``glance.js::sessionStatusBits``), as the same
session's Board row. On a phone the line stays one line, and "up uptime" is the
first part dropped when it will not fit.

Hermetic: the sessions poll and the Board are route-mocked before ``goto()``
(#510), with fictional sessions.
"""

from __future__ import annotations

import json as _json
import re
import time

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.waits import stable_eval

pytestmark = pytest.mark.smoke

_NOW = int(time.time())
_UP = 100_000  # 1d 3h


def _session(sid: str, folder: str, status, age, **extra) -> dict:
    s = {"session_id": sid, "kind": "pty", "agent": "claude", "label": "",
         "project_dir": "E:/work/" + folder, "name": sid, "flags": "",
         "started_at": _NOW - _UP, "alive": True, "rows": 40, "cols": 120,
         "live_title": "", "prompt_title": sid + " title", "manual_title": "",
         "output_chars": 10,
         "board_column": None if status is None else "claude_turn",
         "board_status": status, "board_age_seconds": age}
    s.update(extra)
    return s


def _card(sid: str, folder: str, status: str, age: int) -> dict:
    return {"session_id": sid, "kind": "pty", "agent": "claude", "alive": True,
            "project_dir": "E:/work/" + folder, "name": sid, "live_title": "",
            "prompt_title": sid + " title", "project": folder, "status": status,
            "age_seconds": age, "started_at": "2026-09-10T11:00:00Z"}


_BOARD = {
    "generated_at": "2026-09-10T12:00:00Z",
    "columns": {
        "backlog": [],
        "claude_turn": [_card("s-work", "demo-garden", "working", 540),
                        _card("s-done", "demo-garden", "idle-finished", 68400)],
        "your_turn": [], "other": [], "done": [],
    },
    "github": {"fetched_at": "2026-09-10T12:00:00Z", "error": None},
    "live_sessions": {"available": True, "error": None},
    "sessions_state": {"available": True, "stale": False, "updated_at": None},
    "active_issues": {"available": True, "updated_at": None, "count": 0},
}

_SESSIONS = [
    _session("s-work", "demo-garden", "working", 540),
    _session("s-done", "demo-garden", "idle-finished", 68400),
    _session("s-unknown", "demo-garden", None, None),
]


def _mock(page: Page, sessions: list[dict]) -> None:
    def json_route(pattern: str, body: dict) -> None:
        page.route(re.compile(pattern), lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(body)))
    json_route(r".*/api/claude-code/sessions$", {"sessions": sessions})
    json_route(r".*/api/board(\?.*)?$", _BOARD)
    json_route(r".*/api/board/chief-plan$", {"state": "empty"})
    json_route(r".*/api/claude-code/git-status$", {"projects": []})
    json_route(r".*/api/board/github/refresh$", {"fetched_at": None, "error": None})
    json_route(r".*/api/rate-limits$", {"quota_lines": []})


def _code_row(page: Page, sid: str):
    return page.locator(f'#sessionsList li[data-session-id="{sid}"]')


def test_code_row_reads_the_boards_status_and_age(
    authed_page: Page, base_url: str
) -> None:
    _mock(authed_page, _SESSIONS)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    meta = {sid: _code_row(authed_page, sid).locator(".srow-meta-text")
            for sid in ("s-work", "s-done", "s-unknown")}
    expect(meta["s-work"]).to_have_text("demo-garden · working · 9m · up 1d 3h")
    expect(meta["s-done"]).to_have_text("demo-garden · finished · 19h · up 1d 3h")
    # No Board placement: no status and no age to guess at, just the uptime.
    expect(meta["s-unknown"]).to_have_text("demo-garden · up 1d 3h")

    # The same session's Board row says the same thing after the folder.
    authed_page.locator("#tabBoard").click()
    board_meta = authed_page.locator(
        '#boardColumns li.board-item[data-session-id="s-work"] .board-card-meta')
    expect(board_meta).to_have_text("demo-garden · working · 9m")


@pytest.mark.iphone
def test_code_row_meta_stays_one_line_and_drops_up_first_at_390px(
    authed_page: Page, base_url: str
) -> None:
    authed_page.set_viewport_size({"width": 390, "height": 844})
    _mock(authed_page, [
        _session("s-short", "demo", "idle", 540),
        _session("s-long", "demo-garden", "working", 540),
    ])
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    read = """el => {
      if (!el.isConnected) return null;
      const meta = el.querySelector('.srow-meta');
      const text = el.querySelector('.srow-meta-text');
      const tail = el.querySelector('.srow-meta-tail');
      if (!text.clientWidth) return null;
      const line = parseFloat(getComputedStyle(meta).lineHeight);
      return {oneLine: meta.getBoundingClientRect().height <= line + 1,
              cut: text.scrollWidth > text.clientWidth,
              tailShown: !!tail && !tail.hidden};
    }"""
    # Room to spare: the whole line, "up" included.
    expect(_code_row(authed_page, "s-short").locator(".srow-meta-text")).to_have_text(
        "demo · idle · 9m · up 1d 3h")
    short = stable_eval(_code_row(authed_page, "s-short"), read)
    assert short == {"oneLine": True, "cut": False, "tailShown": True}, short

    # No room: "up" goes first and the rest is whole, not ellipsized.
    long_row = _code_row(authed_page, "s-long")
    expect(long_row.locator(".srow-meta-tail")).to_be_hidden()
    expect(long_row.locator(".srow-meta-text")).to_contain_text(
        "demo-garden · working · 9m")
    long = stable_eval(long_row, read)
    assert long == {"oneLine": True, "cut": False, "tailShown": False}, long
