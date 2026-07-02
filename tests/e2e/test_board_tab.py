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

import copy
import json as _json
import re
from datetime import datetime, timedelta, timezone

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )

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


def _board_payload(gh_age_seconds: int = 0) -> dict:
    """_FAKE_BOARD with ``fetched_at`` stamped relative to the real clock —
    fresh by default so opening the tab does not trigger the stale-cache
    auto-refresh; pass a large age to test that it does."""
    payload = copy.deepcopy(_FAKE_BOARD)
    payload["github"]["fetched_at"] = _iso_utc(
        datetime.now(timezone.utc) - timedelta(seconds=gh_age_seconds)
    )
    return payload


def _mock_board(page: Page, payload: dict | None = None) -> None:
    body = _json.dumps(payload or _board_payload())
    page.route(
        re.compile(r".*/api/board$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=body,
        ),
    )
    # Default stub for the gh-refresh POST so an auto-refresh can never
    # escape to the real server (and its real gh subprocess). Tests that
    # care about the POST register their own capturing route *after* this
    # one — Playwright matches the most recently added route first.
    page.route(
        re.compile(r".*/api/board/github/refresh$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(
                {"fetched_at": _iso_utc(datetime.now(timezone.utc)), "error": None}
            ),
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


def test_board_auto_refreshes_stale_github_on_open(
    authed_page: Page, base_url: str
) -> None:
    """Opening the tab with a gh cache older than the client's staleness
    window (2 min) fires one automatic refresh POST — no ↻ tap needed."""
    _mock_board(authed_page, _board_payload(gh_age_seconds=15 * 60))

    posts: list[str] = []

    def _capture(route):
        posts.append(route.request.method)
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(
                {"fetched_at": _iso_utc(datetime.now(timezone.utc)), "error": None}
            ),
        )

    authed_page.route(re.compile(r".*/api/board/github/refresh$"), _capture)

    _open_board(authed_page, base_url)
    authed_page.wait_for_timeout(1_000)

    assert posts == ["POST"], (
        f"stale gh cache should auto-refresh exactly once on tab open, got {posts}"
    )


def test_board_fresh_github_not_refreshed_on_open(
    authed_page: Page, base_url: str
) -> None:
    """A fresh cache must NOT auto-refresh — tab-open stays free."""
    _mock_board(authed_page, _board_payload(gh_age_seconds=0))

    posts: list[str] = []
    authed_page.route(
        re.compile(r".*/api/board/github/refresh$"),
        lambda route: (posts.append(route.request.method), route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"fetched_at": None, "error": None}),
        )),
    )

    _open_board(authed_page, base_url)
    authed_page.wait_for_timeout(1_000)

    assert posts == [], f"fresh gh cache must not auto-refresh on open, got {posts}"


def test_board_strip_click_scrolls_carousel_not_page(
    authed_page: Page, base_url: str
) -> None:
    """Tapping a strip button pans the carousel horizontally without moving
    the page vertically (the scrollIntoView fly-up bug found on the phone).
    Carousel only exists on the phone projection — desktop shows the grid."""
    viewport = authed_page.viewport_size or {"width": 0}
    if viewport["width"] >= 700:
        pytest.skip("carousel is phone-projection-only; desktop uses the grid")

    _mock_board(authed_page)
    _open_board(authed_page, base_url)

    scroll_y_before = authed_page.evaluate("window.scrollY")
    authed_page.locator("#boardColDone").click()
    authed_page.wait_for_function(
        "document.getElementById('boardColumns').scrollLeft > 0", timeout=5_000
    )
    assert authed_page.evaluate("window.scrollY") == scroll_y_before, (
        "strip tap scrolled the page vertically (fly-up regression)"
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
