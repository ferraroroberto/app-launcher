"""Board tab e2e (issue #300 / #164).

Browser-side coverage: the fifth tab renders the four kanban columns from a
route-mocked ``/api/board`` payload, the strip shows per-column counts (with
the Your-turn attention highlight), the ↻ button POSTs the gh refresh, and
the phone projection lays the columns out as a one-column-per-viewport
carousel while desktop gets the four-column grid. Hermetic — the board API
is route-mocked like the Jobs / Life OS e2e tests.

Server-side logic (cwd join, jobs scan, gh cache/degradation) is covered by
the in-process suite in tests/test_board.py.
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

_FAKE_BOARD = {
    "generated_at": "2026-07-02T12:00:00Z",
    "columns": {
        "backlog": [
            {"kind": "issue", "repo": "app-launcher", "number": 301,
             "title": "Board tab 2/3: drill-down + reply",
             "url": "https://github.com/ferraroroberto/app-launcher/issues/301",
             "updated_at": "2026-07-01T10:00:00Z", "labels": ["enhancement"]},
        ],
        "claude_turn": [
            {"session_id": "s-work", "kind": "pty", "agent": "claude",
             "project_dir": "E:/automation/life-os", "name": "life-os",
             "alive": True, "started_at": "2026-07-02T11:56:00Z",
             "live_title": "weekly recap", "prompt_title": "",
             "project": "life-os", "status": "working", "age_seconds": 240},
        ],
        "your_turn": [
            {"session_id": "s-wait", "kind": "pty", "agent": "claude",
             "project_dir": "E:/automation/photo-ocr", "name": "photo-ocr",
             "alive": True, "started_at": "2026-07-02T11:30:00Z",
             "live_title": "chunk merge fix", "prompt_title": "",
             "project": "photo-ocr", "status": "needs-you", "age_seconds": 720},
            {"kind": "pr", "repo": "app-launcher", "number": 158,
             "title": "keyboard-aware overlay",
             "url": "https://github.com/ferraroroberto/app-launcher/pull/158",
             "updated_at": "2026-07-02T09:00:00Z", "is_draft": False},
            {"kind": "job", "job_id": "reporting", "job_name": "reporting pipeline",
             "state": "failed", "run_id": "20260702T090200",
             "finished_at": "2026-07-02T09:02:00", "age_seconds": 10680},
        ],
        "done": [
            {"kind": "pr", "repo": "voice-transcriber", "number": 88,
             "title": "read-aloud segmentation",
             "url": "https://github.com/ferraroroberto/voice-transcriber/pull/88",
             "updated_at": "2026-07-02T08:00:00Z", "state": "merged",
             "is_draft": False},
        ],
    },
    "github": {"fetched_at": "2026-07-02T11:00:00Z", "error": None},
    "sessions_state": {"available": True, "stale": False,
                       "updated_at": "2026-07-02T11:58:00Z"},
}


def _mock_board(page: Page, payload: dict | None = None) -> None:
    body = _json.dumps(payload or _FAKE_BOARD)
    page.route(
        re.compile(r".*/api/board$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=body,
        ),
    )


def _open_board(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#tabBoard", state="attached", timeout=5_000)
    page.locator("#tabBoard").click()
    expect(page.locator("#paneBoard")).to_be_visible()


def test_board_renders_columns_counts_and_cards(
    authed_page: Page, base_url: str
) -> None:
    _mock_board(authed_page)
    _open_board(authed_page, base_url)

    # Per-column counts on the strip; Your turn (3) carries the attention mark.
    expect(authed_page.locator("#boardColBacklog .board-count")).to_have_text("1")
    expect(authed_page.locator("#boardColClaude .board-count")).to_have_text("1")
    expect(authed_page.locator("#boardColYours .board-count")).to_have_text("3")
    expect(authed_page.locator("#boardColDone .board-count")).to_have_text("1")
    expect(authed_page.locator("#boardColYours")).to_have_class(
        re.compile(r"\battention\b")
    )

    # Your-turn cards: needs-you session, open PR, failed job — in that order.
    yours = authed_page.locator('.board-list[data-col="your_turn"] li.board-item')
    expect(yours.first).to_be_visible(timeout=5_000)
    assert yours.count() == 3
    expect(yours.nth(0)).to_contain_text("photo-ocr")
    expect(yours.nth(0)).to_contain_text("needs you")
    expect(yours.nth(0)).to_contain_text("chunk merge fix")
    expect(yours.nth(1)).to_contain_text("PR #158")
    expect(yours.nth(2)).to_contain_text("failed")

    # Backlog card is repo · #N · title; done card carries the merged state.
    backlog = authed_page.locator('.board-list[data-col="backlog"] li.board-item')
    expect(backlog.first).to_contain_text("app-launcher #301")
    done = authed_page.locator('.board-list[data-col="done"] li.board-item')
    expect(done.first).to_contain_text("merged")


def test_board_refresh_button_posts_gh_refresh(
    authed_page: Page, base_url: str
) -> None:
    _mock_board(authed_page)

    captured: dict = {}

    def _capture(route):
        captured["method"] = route.request.method
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"fetched_at": "2026-07-02T12:05:00Z", "error": None}),
        )

    authed_page.route(re.compile(r".*/api/board/github/refresh$"), _capture)

    _open_board(authed_page, base_url)
    authed_page.locator("#boardRefresh").click()
    authed_page.wait_for_timeout(400)

    assert captured.get("method") == "POST", (
        "↻ never POSTed /api/board/github/refresh"
    )


def test_board_columns_layout_matches_projection(
    authed_page: Page, base_url: str
) -> None:
    """Phone (WebKit / iPhone projection): the carousel shows one column per
    viewport — a column spans ~the full container width. Desktop (Chromium,
    fine pointer ≥700px): the grid shows all four columns — each column is
    at most ~a third of the container. Same DOM, projection-dependent CSS."""
    _mock_board(authed_page)
    _open_board(authed_page, base_url)

    container = authed_page.locator("#boardColumns")
    first_col = authed_page.locator(".board-col").first
    expect(first_col).to_be_attached()

    box_container = container.bounding_box()
    box_col = first_col.bounding_box()
    assert box_container and box_col, "board columns not laid out"

    viewport = authed_page.viewport_size or {"width": 0}
    if viewport["width"] < 700:
        assert box_col["width"] >= box_container["width"] * 0.9, (
            f"phone column should fill the viewport: col={box_col['width']}, "
            f"container={box_container['width']}"
        )
    else:
        assert box_col["width"] <= box_container["width"] * 0.35, (
            f"desktop column should sit in a 4-col grid: col={box_col['width']}, "
            f"container={box_container['width']}"
        )
