"""The Code tab's session rows carry the Board's status and age (issue #1580).

A row reads "folder · status", then the figures "age" and "up uptime" in the
vendored meta tail: the same words, from the same shared formatter
(``glance.js::sessionStatusParts``), as the same session's Board row. On a
phone the line stays one line and the figures stay whole: the folder, then the
model, are what give way.

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

# 27h30m: half an hour clear of the 1d 3h / 1d 4h boundary, and stamped per
# request in ``_mock`` so the label never depends on when the module loaded.
_UP = 99_000  # 1d 3h


def _session(sid: str, folder: str, status, age, **extra) -> dict:
    s = {"session_id": sid, "kind": "pty", "agent": "claude", "label": "",
         "project_dir": "E:/work/" + folder, "name": sid, "flags": "",
         "started_at": None, "alive": True, "rows": 40, "cols": 120,
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
    _session("s-work", "demo-garden", "working", 540, model="Opus"),
    _session("s-done", "demo-garden", "idle-finished", 68400, model="Sonnet"),
    _session("s-unknown", "demo-garden", None, None),
]


def _mock(page: Page, sessions: list[dict]) -> None:
    def json_route(pattern: str, body: dict) -> None:
        page.route(re.compile(pattern), lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(body)))
    page.route(re.compile(r".*/api/claude-code/sessions$"), lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=_json.dumps({"sessions": [
            {**s, "started_at": int(time.time()) - _UP} for s in sessions]})))
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
    expect(meta["s-work"]).to_have_text("demo-garden · Opus · working")
    expect(meta["s-done"]).to_have_text("demo-garden · Sonnet · finished")
    tails = {sid: _code_row(authed_page, sid).locator(".action-row-meta-tail")
             for sid in ("s-work", "s-done", "s-unknown")}
    expect(tails["s-work"]).to_have_text(["9m", "up 1d 3h"])
    expect(tails["s-done"]).to_have_text(["19h", "up 1d 3h"])
    # No Board placement: no status and no age to guess at, just the uptime.
    # No model either: the segment is omitted, never a placeholder.
    expect(meta["s-unknown"]).to_have_text("demo-garden")
    expect(tails["s-unknown"]).to_have_text(["up 1d 3h"])

    # The same session's Board row says the same thing after the folder.
    authed_page.locator("#tabBoard").click()
    board_meta = authed_page.locator(
        '#boardColumns li.board-item[data-session-id="s-work"] .board-card-meta')
    expect(board_meta.locator(".action-row-meta-text")).to_have_text("demo-garden · working")
    expect(board_meta.locator(".action-row-meta-tail")).to_have_text("9m")


@pytest.mark.iphone
def test_code_row_meta_stays_one_line_and_keeps_its_figures_whole_at_390px(
    authed_page: Page, base_url: str
) -> None:
    authed_page.set_viewport_size({"width": 390, "height": 844})
    _mock(authed_page, [
        _session("s-short", "demo", "idle", 540),
        _session("s-long", "demo-garden", "working", 540, model="Sonnet"),
        _session("s-tight", "demo-garden-planner", "working", 540, model="Sonnet"),
        # The folder is at its minimum width and the line still runs over by a
        # figure or two: the model goes, a figure is never clipped (LAYOUT-09).
        _session("s-edge", "dem", "idle-finished", 68400, model="Opus"),
    ])
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    read = """el => {
      if (!el.isConnected) return null;
      const meta = el.querySelector('.srow-meta');
      const rest = el.querySelector('.srow-meta-rest');
      const tails = Array.from(el.querySelectorAll('.action-row-meta-tail'));
      if (!meta.clientWidth) return null;
      const line = parseFloat(getComputedStyle(meta).lineHeight);
      const edge = meta.getBoundingClientRect().right + 1;
      return {oneLine: meta.getBoundingClientRect().height <= line + 1,
              restCut: rest ? rest.scrollWidth > rest.clientWidth : false,
              tails: tails.length,
              tailsWhole: tails.every(t => t.getBoundingClientRect().right <= edge
                && t.scrollWidth <= t.clientWidth + 1)};
    }"""
    # Room to spare: the whole line, figures included.
    expect(_code_row(authed_page, "s-short").locator(".srow-meta-text")).to_have_text(
        "demo · idle")
    expect(_code_row(authed_page, "s-short").locator(".action-row-meta-tail")).to_have_text(
        ["9m", "up 1d 3h"])
    short = stable_eval(_code_row(authed_page, "s-short"), read)
    assert short == {"oneLine": True, "restCut": False, "tails": 2, "tailsWhole": True}, short

    # The figures stay whole on every row, whatever the folder and model do.
    for sid in ("s-edge", "s-long", "s-tight"):
        row = _code_row(authed_page, sid)
        expect(row.locator(".action-row-meta-tail")).to_have_count(2)
        got = stable_eval(row, read)
        assert got["oneLine"] is True and got["tailsWhole"] is True, (sid, got)
        assert got["restCut"] is False, (sid, got)

    # No room: only the folder is cut (ellipsized); the status stays whole.
    tight = _code_row(authed_page, "s-tight")
    head = stable_eval(tight, """el => {
      const h = el.querySelector('.srow-meta-head');
      return h.clientWidth ? h.scrollWidth > h.clientWidth : null;
    }""")
    assert head is True, "the folder, not the status or a figure, is what a phone cuts"
