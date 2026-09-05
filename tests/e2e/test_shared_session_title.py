"""#396: the Board tab's session cards and the Coding tab's Running-sessions
list must show an identical title for the same live session, sourced from
the shared ``shared_name`` field (fleet-config#302) both ``GET
/api/claude-code/sessions`` and ``GET /api/board`` now carry (joined by the
same agent-aware claim walk server-side — see ``src/board.py``'s ``attach_shared_names``/
``merge_sessions``).

Hermetic like ``test_board_tab.py``: both endpoints are route-mocked with a
fixture sharing one ``session_id``/``shared_name`` pair, so this pins the
*frontend* contract (both tabs must call the same ``sessionTitle()``) without
depending on the real session-host or hook state file.
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

_SID = "s-shared"
_SHARED_NAME = "Fixing the chunk merge bug"

_FAKE_SESSIONS = {
    "sessions": [
        {
            "session_id": _SID,
            "kind": "pty",
            "agent": "claude",
            "project_dir": "E:/automation/photo-ocr",
            "name": "photo-ocr",
            "alive": True,
            "started_at": "2026-07-08T11:30:00Z",
            "live_title": "",
            "prompt_title": "",
            "shared_name": _SHARED_NAME,
            "shared_name_source": None,
        }
    ]
}

_FAKE_BOARD = {
    "generated_at": "2026-07-08T12:00:00Z",
    "columns": {
        "backlog": [],
        "claude_turn": [
            {
                "session_id": _SID,
                "kind": "pty",
                "agent": "claude",
                "project_dir": "E:/automation/photo-ocr",
                "name": "photo-ocr",
                "alive": True,
                "started_at": "2026-07-08T11:30:00Z",
                "live_title": "",
                "prompt_title": "",
                "shared_name": _SHARED_NAME,
                "shared_name_source": None,
                "project": "photo-ocr",
                "status": "working",
                "age_seconds": 300,
            },
        ],
        "your_turn": [],
        "other": [],
        "done": [],
    },
    "github": {"fetched_at": "2026-07-08T11:00:00Z", "error": None},
    "sessions_state": {"available": True, "stale": False,
                       "updated_at": "2026-07-08T11:58:00Z"},
}


def _mock_sessions(page: Page) -> None:
    page.route(
        re.compile(r".*/api/claude-code/sessions$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(_FAKE_SESSIONS),
        ),
    )


def _mock_board(page: Page) -> None:
    page.route(
        re.compile(r".*/api/board(?:\?.*)?$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(_FAKE_BOARD),
        ),
    )
    page.route(
        re.compile(r".*/api/board/github/refresh$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"fetched_at": "2026-07-08T12:00:00Z", "error": None}),
        ),
    )
    # Boot-time git-status is git-subprocess-backed and re-renders the Board
    # on arrival (#510/#680) — keep it off the real server and deterministic.
    page.route(
        re.compile(r".*/api/claude-code/git-status$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"projects": []}),
        ),
    )


def test_board_and_coding_tab_show_identical_shared_title(
    authed_page: Page, base_url: str
) -> None:
    _mock_sessions(authed_page)
    _mock_board(authed_page)

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    # Coding tab is the default landing pane — Running-sessions row title.
    coding_row = authed_page.locator(
        '#sessionsList li[data-session-id="' + _SID + '"] .name'
    )
    expect(coding_row).to_have_text(_SHARED_NAME, timeout=10_000)

    # Board tab's card for the same session_id.
    authed_page.locator("#tabBoard").click()
    expect(authed_page.locator("#paneBoard")).to_be_visible()
    board_card = authed_page.locator(
        '.board-list[data-col="claude_turn"] .board-card-title'
    ).first
    expect(board_card).to_have_text(_SHARED_NAME, timeout=10_000)
    expect(
        authed_page.locator(
            '.board-list[data-col="claude_turn"] .board-agent-icon'
        ).first
    ).to_have_attribute("alt", "Claude Code")
