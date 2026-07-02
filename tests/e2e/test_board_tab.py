"""Board tab e2e (issues #300 / #301 / #302 / #164).

Browser-side coverage: the fifth tab renders the four kanban columns from a
route-mocked ``/api/board`` payload, the strip shows per-column counts (with
the Your-turn attention highlight), the ↻ button POSTs the gh refresh, and
the phone projection lays the columns out as a one-column-per-viewport
carousel while desktop gets the four-column grid. The #302 dispatch bar
POSTs {repo, goal, mode} and keeps its goal for rapid multi-dispatch, and
the dictation mics (dispatch bar + drawer reply box) render when the server
reports voice dictation available. Hermetic — the board API is route-mocked
like the Jobs / Life OS e2e tests.

Server-side logic (cwd join, jobs scan, gh cache/degradation, the
spawn-then-type dispatch endpoint) is covered by the in-process suite in
tests/test_board.py + tests/test_board_dispatch.py.
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
             "is_draft": False, "closes": [87]},
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
    # A merged PR that closed an issue names it (server-side pairing) so
    # Done never doubles PR + issue for one unit of work.
    expect(done.first).to_contain_text("closes #87")


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


_FAKE_EXCHANGE = {
    "available": True,
    "user": {"text": "please fix the merge", "timestamp": "2026-07-02T11:50:00Z"},
    "assistant": {
        "text": "Merge fixed — tests green. Ship it?",
        "timestamp": "2026-07-02T11:55:00Z",
    },
}


def _mock_exchange(page: Page, sid: str = "s-wait") -> None:
    page.route(
        re.compile(r".*/api/board/sessions/" + sid + r"/exchange.*"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(_FAKE_EXCHANGE),
        ),
    )


def test_board_card_drawer_shows_exchange_and_posts_reply(
    authed_page: Page, base_url: str
) -> None:
    """#301: tapping a session card opens the drawer with the last exchange;
    ➤ posts the reply body {data, submit: true} to the input proxy."""
    _mock_board(authed_page)
    _mock_exchange(authed_page)

    captured: dict = {}

    def _capture_input(route):
        captured["method"] = route.request.method
        captured["body"] = route.request.post_data_json
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"ok": True, "bytes": 8, "submit": True}),
        )

    authed_page.route(
        re.compile(r".*/api/claude-code/sessions/s-wait/input$"), _capture_input
    )

    _open_board(authed_page, base_url)
    card = authed_page.locator(
        '.board-list[data-col="your_turn"] li.board-item'
    ).first.locator("button.board-card")
    card.click()

    drawer = authed_page.locator(".board-drawer")
    expect(drawer).to_be_visible()
    expect(drawer).to_contain_text("please fix the merge")
    expect(drawer).to_contain_text("Merge fixed — tests green. Ship it?")

    # The drawer stacks BELOW the card at (almost) full card width — never
    # splits it horizontally (phone feedback on #301).
    box_card = card.bounding_box()
    box_drawer = drawer.bounding_box()
    assert box_card and box_drawer, "card/drawer not laid out"
    assert box_drawer["y"] >= box_card["y"] + box_card["height"] - 2, (
        "drawer must render below the card, not beside it"
    )
    assert box_drawer["width"] >= box_card["width"] * 0.9, (
        "drawer must span the card's width"
    )

    authed_page.locator(".board-reply-input").fill("go ahead")
    authed_page.locator(".board-reply-send").click()
    authed_page.wait_for_timeout(500)

    assert captured.get("method") == "POST"
    assert captured.get("body") == {"data": "go ahead", "submit": True}


def test_backlog_start_button_posts_issue_start(
    authed_page: Page, base_url: str
) -> None:
    """#301: a backlog card of a repo present in the projects folder carries
    ▶ Start, which posts the server-validated {repo, number, mode}."""
    # The ▶/⚡ buttons only render for repos the Coding tab could launch in —
    # mock /api/apps so 'app-launcher' (the fake issue's repo) qualifies.
    authed_page.route(
        re.compile(r".*/api/apps$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"scan_root": "", "apps": [{
                "id": "cc-app-launcher", "kind": "claude-code",
                "name": "app-launcher",
                "project_dir": "E:/automation/app-launcher",
            }]}),
        ),
    )
    _mock_board(authed_page)

    captured: dict = {}

    def _capture_start(route):
        captured["body"] = route.request.post_data_json
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "launched": "/issue-start 301", "repo": "app-launcher",
                "session": {"session_id": "sX", "kind": "pty",
                            "name": "app-launcher"},
            }),
        )

    authed_page.route(re.compile(r".*/api/board/issues/start$"), _capture_start)

    _open_board(authed_page, base_url)
    authed_page.locator("#boardColBacklog").click()
    start_btn = authed_page.locator(
        '.board-list[data-col="backlog"] .board-issue-btn'
    ).first
    # The buttons render only once boot's /api/apps fetch has populated
    # state.apps; on a slow runner the first board render can precede it
    # (seen on CI). The 5 s poll re-renders with apps loaded, so a budget
    # spanning a full poll cycle makes this deterministic.
    expect(start_btn).to_be_visible(timeout=15_000)
    start_btn.click()
    authed_page.wait_for_timeout(500)

    body = captured.get("body") or {}
    assert body.get("repo") == "app-launcher"
    assert body.get("number") == 301
    assert body.get("mode") == "start"


def test_board_deep_link_opens_drawer(authed_page: Page, base_url: str) -> None:
    """#301: ?board=<sid> lands on the Board with that card's drawer open —
    the target of the Slack-ping deep link."""
    _mock_board(authed_page)
    _mock_exchange(authed_page)

    authed_page.goto(f"{base_url}/?board=s-wait", wait_until="domcontentloaded")
    expect(authed_page.locator("#paneBoard")).to_be_visible(timeout=10_000)
    drawer = authed_page.locator(".board-drawer")
    expect(drawer).to_be_visible(timeout=10_000)
    expect(drawer).to_contain_text("Merge fixed — tests green. Ship it?")


def _mock_apps_with_app_launcher(page: Page) -> None:
    """state.apps with one claude-code entry, so the dispatch repo select
    (and the #301 ▶/⚡ buttons) have a launchable repo."""
    page.route(
        re.compile(r".*/api/apps$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"scan_root": "", "apps": [{
                "id": "cc-app-launcher", "kind": "claude-code",
                "name": "app-launcher",
                "project_dir": "E:/automation/app-launcher",
            }]}),
        ),
    )


def test_dispatch_bar_posts_repo_mode_goal_and_keeps_text(
    authed_page: Page, base_url: str
) -> None:
    """#302: goal + repo + mode ride POST /api/board/dispatch; the goal text
    survives the send (populated-but-clearable for rapid multi-dispatch)."""
    _mock_apps_with_app_launcher(authed_page)
    _mock_board(authed_page)

    captured: dict = {}

    def _capture_dispatch(route):
        captured["method"] = route.request.method
        captured["body"] = route.request.post_data_json
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "launched": "/issue-yolo ship the goal bar",
                "repo": "app-launcher",
                "session": {"session_id": "sD", "kind": "pty",
                            "name": "app-launcher"},
            }),
        )

    authed_page.route(re.compile(r".*/api/board/dispatch$"), _capture_dispatch)

    _open_board(authed_page, base_url)
    # The repo select fills once boot's /api/apps fetch lands; the board
    # render re-syncs it, so a full poll cycle is the worst case.
    expect(
        authed_page.locator('#boardDispatchRepo option[value="app-launcher"]')
    ).to_have_count(1, timeout=15_000)

    authed_page.locator("#boardDispatchGoal").fill("ship the goal bar")
    authed_page.locator('.board-mode-btn[data-mode="yolo"]').click()
    authed_page.locator("#boardDispatchSend").click()
    authed_page.wait_for_timeout(500)

    assert captured.get("method") == "POST"
    body = captured.get("body") or {}
    assert body.get("repo") == "app-launcher"
    assert body.get("goal") == "ship the goal bar"
    assert body.get("mode") == "yolo"
    assert body.get("opus") is False
    # Populated-but-clearable: the goal stays after a successful send.
    expect(authed_page.locator("#boardDispatchGoal")).to_have_value(
        "ship the goal bar"
    )
    authed_page.locator("#boardDispatchClear").click()
    expect(authed_page.locator("#boardDispatchGoal")).to_have_value("")


def test_dispatch_and_reply_mics_render_when_voice_available(
    authed_page: Page, base_url: str
) -> None:
    """#302: with the server reporting voice dictation available (and
    MediaRecorder present), the 🎤 shows on the dispatch bar and inside the
    drawer's reply row."""
    # voiceAvailable() also needs window.MediaRecorder, absent in headless
    # WebKit — a bare stub is enough (presence check only, no recording).
    authed_page.add_init_script(
        "if (!window.MediaRecorder) { window.MediaRecorder = class {}; }"
    )

    def _status_voice_on(route):
        resp = route.fetch()
        body = resp.json()
        body["voice_dictation"] = True
        route.fulfill(response=resp, json=body)

    authed_page.route(re.compile(r".*/api/status$"), _status_voice_on)
    _mock_board(authed_page)
    _mock_exchange(authed_page)

    _open_board(authed_page, base_url)
    # The board render re-syncs mic visibility once /api/status has landed.
    expect(authed_page.locator("#boardDispatchRecord")).to_be_visible(
        timeout=15_000
    )

    authed_page.locator(
        '.board-list[data-col="your_turn"] li.board-item'
    ).first.locator("button.board-card").click()
    drawer = authed_page.locator(".board-drawer")
    expect(drawer).to_be_visible()
    expect(drawer.locator(".board-reply-record")).to_be_visible()


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
