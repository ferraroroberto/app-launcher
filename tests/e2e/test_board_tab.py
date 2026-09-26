"""Board tab e2e (issues #300 / #301 / #302 / #164 / #399).

Browser-side coverage: the fifth tab renders the five single-purpose kanban
columns from a route-mocked ``/api/board`` payload as collapsible section
cards (#1198) whose summaries carry per-column counts (with the Your-turn
attention highlight), the ↻ button POSTs the gh refresh, and the phone
projection stacks the sections while desktop gets the five-column grid.
The #302 dispatch bar POSTs {repo, goal, mode} and keeps its goal for rapid
multi-dispatch, and the dictation mics (dispatch bar + the drawer's shared
composer, #984)
render when the server reports voice dictation available. Hermetic — the
board API is route-mocked like the Jobs / Life OS e2e tests.

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

from tests.e2e._contrast import contrast_ratio
from tests.e2e.conftest import stable_read

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
             "project": "photo-ocr", "status": "awaiting-input", "age_seconds": 720},
        ],
        "other": [
            {"kind": "pr", "repo": "app-launcher", "number": 158,
             "title": "keyboard-aware overlay",
             "url": "https://github.com/ferraroroberto/app-launcher/pull/158",
             "updated_at": "2026-07-02T09:00:00Z", "is_draft": False},
            {"kind": "job", "job_id": "reporting", "job_name": "reporting pipeline",
             "state": "failed", "run_id": "20260702T090200",
             "finished_at": "2026-07-02T09:02:00", "age_seconds": 10680},
        ],
        "done": [
            {"kind": "issue", "repo": "voice-transcriber", "number": 87,
             "title": "read-aloud segmentation",
             "url": "https://github.com/ferraroroberto/voice-transcriber/issues/87",
             "updated_at": "2026-07-02T08:00:00Z", "state": "closed", "labels": []},
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
        re.compile(r".*/api/board(?:\?.*)?$"),
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
    # Same reasoning for the boot-time git-status fetch, which is backed by a
    # real `git` subprocess per project and so lands whenever it likes; its
    # completion calls renderBoard() whenever the Board tab is up
    # (apps-coding.js::refreshGitStatus), rebuilding the card DOM
    # mid-test (#510/#680). Clean payload so it can't perturb the rendered
    # annotations other tests read.
    # test_board_chief.py's own _mock_board already does this; the two tests
    # below that care about the response register their route *after* this.
    page.route(
        re.compile(r".*/api/claude-code/git-status$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"projects": []}),
        ),
    )


def _open_board(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#tabBoard", state="attached", timeout=5_000)
    page.locator("#tabBoard").click()
    expect(page.locator("#paneBoard")).to_be_visible()


def _unfold(page: Page, *section_ids: str) -> None:
    """Open Board column sections (#1198). The GitHub-fed ones start folded on
    the phone and open on the desktop grid, so tap a summary only when its
    section is still closed — a tap on an open one would fold it."""
    for section_id in section_ids:
        section = page.locator(f"#{section_id}")
        if section.get_attribute("open") is None:
            page.locator(f"#{section_id} > summary").click()
        expect(section).to_have_attribute("open", "")


@pytest.mark.iphone
def test_board_renders_columns_counts_and_cards(
    authed_page: Page, base_url: str
) -> None:
    """One render of the mocked board, checked top to bottom (#1215 merged
    the section-structure and column-geometry tests in here): the section
    cards and their default open state, counts, the Your-turn card, the
    projection's column geometry — all before anything is unfolded — then
    the GitHub-fed columns' cards, and last a fold that must survive a poll.
    """
    _mock_board(authed_page)
    _open_board(authed_page, base_url)

    # -- was test_board_sections_are_collapsible_cards_that_survive_the_poll
    # (structure + default open state) --
    # #1198: the Board is built like every other tab. The first thing under
    # the page header is a section card holding the dispatch bar, each column
    # is a collapsible section card with its count in the summary, and the
    # phone's column strip is gone. A section the user folds stays folded
    # across the 5 s poll: renderBoard() re-renders the lists, never a
    # section's open state (that half is the last step below).
    first = authed_page.locator("#paneBoard > .page-head + *")
    expect(first).to_have_id("boardDispatchCard")
    expect(first).to_have_class(re.compile(r"\bcard--collapsible\b"))
    expect(first.locator("> summary .collapse-title")).to_have_text("Dispatch")
    expect(first.locator("#boardDispatch")).to_have_count(1)

    sections = authed_page.locator("#boardColumns > details.card--collapsible")
    expect(sections).to_have_count(5)
    expect(
        authed_page.locator("#boardColumns > details > summary .board-count")
    ).to_have_count(5)
    expect(authed_page.locator(".board-strip")).to_have_count(0)

    # Phone: the live columns open, the GitHub-fed ones folded; the desktop
    # grid opens all five.
    viewport = authed_page.viewport_size or {"width": 0}
    backlog_section = authed_page.locator("#boardColBacklog")
    if viewport["width"] < 700:
        expect(backlog_section).not_to_have_attribute("open", "")
    else:
        expect(backlog_section).to_have_attribute("open", "")
    yours_section = authed_page.locator("#boardColYours")
    expect(yours_section).to_have_attribute("open", "")

    # -- counts, attention mark and the Your-turn card (this test's own) --
    # Per-column counts in each section's summary; Your turn (1) carries the
    # attention mark.
    expect(authed_page.locator("#boardColBacklog .board-count")).to_have_text("1")
    expect(authed_page.locator("#boardColClaude .board-count")).to_have_text("1")
    expect(authed_page.locator("#boardColYours .board-count")).to_have_text("1")
    expect(authed_page.locator("#boardColOther .board-count")).to_have_text("2")
    expect(authed_page.locator("#boardColDone .board-count")).to_have_text("1")
    expect(authed_page.locator("#boardColYours")).to_have_class(
        re.compile(r"\battention\b")
    )

    # Your-turn holds the needs-you session only (#399: terminal-only column).
    yours = authed_page.locator('.board-list[data-col="your_turn"] li.board-item')
    expect(yours.first).to_be_visible(timeout=5_000)
    assert yours.count() == 1
    expect(yours.nth(0)).to_contain_text("photo-ocr")
    expect(yours.nth(0)).to_contain_text("needs you")
    expect(yours.nth(0)).to_contain_text("chunk merge fix")
    # Title first (#1198): the card's title leads, the project/status meta
    # line follows it, and the clamped title keeps its full text reachable.
    title = yours.nth(0).locator(".board-card > :first-child")
    expect(title).to_have_class(re.compile(r"\bboard-card-title\b"))
    expect(title).to_have_attribute("title", "chunk merge fix")
    expect(yours.nth(0).locator(".board-card > :nth-child(2)")).to_have_class(
        re.compile(r"\bboard-card-meta\b")
    )

    # -- was test_board_columns_layout_matches_projection (measured before any
    # section is unfolded, as that test did) --
    # Phone (WebKit / iPhone projection): the column sections stack like
    # every other tab's cards — each spans ~the full container width, the next
    # one below it (#1198). Desktop (Chromium, fine pointer ≥700px): the grid
    # shows all five columns — each column is well under half the container.
    # Same DOM, projection-dependent CSS.
    container = authed_page.locator("#boardColumns")
    first_col = authed_page.locator(".board-col").first
    expect(first_col).to_be_attached()

    box_container = stable_read(container.bounding_box)
    box_col = stable_read(first_col.bounding_box)
    box_next = stable_read(authed_page.locator(".board-col").nth(1).bounding_box)
    assert box_container and box_col and box_next, "board columns not laid out"

    if viewport["width"] < 700:
        assert box_col["width"] >= box_container["width"] * 0.9, (
            f"phone column should fill the viewport: col={box_col['width']}, "
            f"container={box_container['width']}"
        )
        assert box_next["y"] >= box_col["y"] + box_col["height"], (
            f"phone sections should stack: next y={box_next['y']}, "
            f"first bottom={box_col['y'] + box_col['height']}"
        )
    else:
        assert box_col["width"] <= box_container["width"] * 0.35, (
            f"desktop column should sit in a 5-col grid: col={box_col['width']}, "
            f"container={box_container['width']}"
        )

    # -- the GitHub-fed columns' cards (this test's own) --
    _unfold(authed_page, "boardColBacklog", "boardColOther", "boardColDone")

    # Other holds the open PR + failed job, in that order.
    other = authed_page.locator('.board-list[data-col="other"] li.board-item')
    expect(other.first).to_be_visible(timeout=5_000)
    assert other.count() == 2
    expect(other.nth(0)).to_contain_text("PR #158")
    expect(other.nth(1)).to_contain_text("failed")

    # Backlog card is repo · #N · title; done card is a closed issue.
    backlog = authed_page.locator('.board-list[data-col="backlog"] li.board-item')
    expect(backlog.first).to_contain_text("app-launcher #301")
    done = authed_page.locator('.board-list[data-col="done"] li.board-item')
    expect(done.first).to_contain_text("#87")
    expect(done.first).to_contain_text("closed")

    # -- was test_board_sections_are_collapsible_cards_that_survive_the_poll
    # (the fold that must survive a poll; last: it changes section state) --
    authed_page.locator("#boardColYours > summary").click()
    expect(yours_section).not_to_have_attribute("open", "")
    # Wait out one full poll: its response lands, then its render runs.
    with authed_page.expect_response(
        lambda resp: resp.url.endswith("/api/board"), timeout=15_000
    ):
        pass
    authed_page.wait_for_timeout(300)
    expect(yours_section).not_to_have_attribute("open", "")


@pytest.mark.parametrize("gh_age_seconds, expected_posts", [
    pytest.param(15 * 60, ["POST"], id="stale-refreshes-once"),
    pytest.param(0, [], id="fresh-not-refreshed"),
])
def test_board_github_refresh_on_open_tracks_cache_age(
    authed_page: Page, base_url: str, gh_age_seconds: int,
    expected_posts: list[str],
) -> None:
    """Opening the tab with a gh cache older than the client's staleness
    window (2 min) fires exactly one automatic refresh POST — no ↻ tap
    needed; a fresh cache must NOT auto-refresh, so tab-open stays free.

    Then the ↻ tap (was test_board_refresh_button_posts_gh_refresh, merged
    here in #1215) adds exactly one more POST, in both cache-age cases."""
    _mock_board(authed_page, _board_payload(gh_age_seconds=gh_age_seconds))

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

    assert posts == expected_posts, (
        f"gh cache aged {gh_age_seconds}s should auto-refresh {expected_posts} "
        f"on tab open, got {posts}"
    )

    # -- was test_board_refresh_button_posts_gh_refresh --
    # ↻ is disabled while a refresh is in flight (refreshGithub()), so let
    # the automatic one settle before tapping it.
    expect(authed_page.locator("#boardRefresh")).to_be_enabled()
    authed_page.locator("#boardRefresh").click()
    authed_page.wait_for_timeout(400)

    assert posts == expected_posts + ["POST"], (
        f"↻ never POSTed /api/board/github/refresh (got {posts})"
    )


def _unfetched_payload() -> dict:
    """The board right after a webapp restart (#910): the gh cache has never
    been filled, so Backlog/Done are empty lists that mean "unknown" — while
    the session and job columns keep working."""
    payload = _board_payload()
    payload["github"] = {"available": False, "fetched_at": None, "error": None}
    payload["columns"]["backlog"] = []
    payload["columns"]["done"] = []
    payload["columns"]["other"] = [
        card for card in payload["columns"]["other"] if card["kind"] == "job"
    ]
    return payload


def _route_board_from(page: Page, current: dict) -> None:
    """Serve whatever ``current["body"]`` holds at request time, so a test
    can change the server's answer mid-flight. Registered after _mock_board,
    so it wins."""
    page.route(
        re.compile(r".*/api/board(?:\?.*)?$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(current["body"]),
        ),
    )


def test_board_unfetched_github_renders_unknown_not_zero(
    authed_page: Page, base_url: str
) -> None:
    """#910: a never-fetched gh cache must not read as "zero issues". Backlog
    and Done show an unknown count and a not-loaded message; Other keeps its
    real job count. Once a refresh genuinely comes back empty, the same
    columns show a plain 0 — a real zero must not look like a failure."""
    _mock_board(authed_page)
    current = {"body": _unfetched_payload()}
    _route_board_from(authed_page, current)
    _open_board(authed_page, base_url)

    for section in ("#boardColBacklog", "#boardColDone"):
        expect(authed_page.locator(f"{section} .board-count")).to_have_text("—")
    expect(authed_page.locator("#boardColOther .board-count")).to_have_text("1")
    backlog_empty = authed_page.locator('.board-empty[data-col="backlog"]')
    expect(backlog_empty.locator(".empty-state-message")).to_have_text(
        "Not loaded from GitHub yet.")
    # The lane's one next action is a real control now (#1238 J-09).
    expect(backlog_empty.locator(".empty-state-action")).to_have_text("Refresh")

    loaded_empty = _unfetched_payload()
    loaded_empty["github"] = {
        "available": True,
        "fetched_at": _iso_utc(datetime.now(timezone.utc)),
        "error": None,
    }
    # Opening the tab auto-refreshed the never-fetched cache; wait for that to
    # settle, or this ↻ tap lands on the in-flight guard and is dropped.
    expect(authed_page.locator("#boardRefresh")).to_be_enabled()
    current["body"] = loaded_empty
    authed_page.locator("#boardRefresh").click()

    for section in ("#boardColBacklog", "#boardColDone"):
        expect(authed_page.locator(f"{section} .board-count")).to_have_text("0")
    expect(backlog_empty.locator(".empty-state-message")).to_have_text(
        "No open issues on GitHub.")
    expect(
        authed_page.locator('.board-empty[data-col="done"] .empty-state-message')
    ).to_have_text("Nothing closed today yet.")


def test_board_poll_heals_github_emptied_by_restart(
    authed_page: Page, base_url: str
) -> None:
    """#910: a Board left open while the webapp restarts sees the cache go
    back to never-fetched on its next 5 s poll, and refreshes it by itself
    instead of waiting for a ↻ tap or a tab switch."""
    _mock_board(authed_page)
    current = {"body": _board_payload()}  # fresh: opening the tab stays free
    _route_board_from(authed_page, current)
    _open_board(authed_page, base_url)
    expect(authed_page.locator("#boardColBacklog .board-count")).to_have_text("1")

    with authed_page.expect_request(
        lambda req: req.method == "POST" and req.url.endswith("/api/board/github/refresh"),
        timeout=15_000,
    ):
        current["body"] = _unfetched_payload()


def _blind_payload() -> dict:
    """The board with the session-host unreadable and one job whose run
    history can't be read (#915): both live columns are empty lists that mean
    "unknown", while GitHub keeps rendering."""
    payload = _board_payload()
    payload["live_sessions"] = {"available": False, "error": "session-host unreachable"}
    payload["columns"]["claude_turn"] = []
    payload["columns"]["your_turn"] = []
    payload["columns"]["other"].append(
        {"kind": "job", "job_id": "digest", "job_name": "daily digest",
         "state": "unreadable", "run_id": None, "finished_at": None,
         "age_seconds": None, "error": "[WinError 5] Access is denied"},
    )
    return payload


def test_board_unreadable_sources_render_unknown_not_zero(
    authed_page: Page, base_url: str
) -> None:
    """#915: an unreachable session-host must not read as "Nothing needs you
    right now." — the live columns show an unknown count, a distinct message
    and a status line; a job with unreadable history shows as a card, not as
    nothing. Once the list is read and genuinely empty, the same columns show
    a plain 0 and today's text — a real zero must not look like a failure."""
    _mock_board(authed_page)
    current = {"body": _blind_payload()}
    _route_board_from(authed_page, current)
    _open_board(authed_page, base_url)

    for section in ("#boardColClaude", "#boardColYours"):
        expect(authed_page.locator(f"{section} .board-count")).to_have_text("—")
    yours_empty = authed_page.locator('.board-empty[data-col="your_turn"]')
    expect(yours_empty.locator(".empty-state-message")).to_have_text(
        "Session-host unreachable — sessions unknown.")
    expect(yours_empty.locator(".empty-state-action")).to_have_text("Retry")
    expect(authed_page.locator("#boardColYours")).not_to_have_class(
        re.compile(r"\battention\b")
    )
    expect(authed_page.locator("#boardStatus")).to_contain_text(
        "session-host unreachable"
    )
    expect(authed_page.locator("#boardColBacklog .board-count")).to_have_text("1")
    expect(authed_page.locator("#boardColOther .board-count")).to_have_text("3")
    unreadable = authed_page.locator(
        '.board-list[data-col="other"] li.board-item.is-unreadable'
    )
    expect(unreadable).to_contain_text("job · unreadable")
    expect(unreadable).to_contain_text("daily digest")

    read_empty = _board_payload()
    read_empty["live_sessions"] = {"available": True, "error": None}
    read_empty["columns"]["claude_turn"] = []
    read_empty["columns"]["your_turn"] = []
    expect(authed_page.locator("#boardRefresh")).to_be_enabled()
    current["body"] = read_empty
    authed_page.locator("#boardRefresh").click()

    for section in ("#boardColClaude", "#boardColYours"):
        expect(authed_page.locator(f"{section} .board-count")).to_have_text("0")
    expect(yours_empty.locator(".empty-state-message")).to_have_text(
        "Nothing needs you right now.")
    expect(
        authed_page.locator('.board-empty[data-col="claude_turn"] .empty-state-message')
    ).to_have_text("No sessions on Claude’s side.")
    # Start work lands in the dispatch bar, ready to type (#1238 J-09).
    yours_empty.locator(".empty-state-action").click()
    expect(authed_page.locator("#boardDispatchGoal")).to_be_focused()
    expect(authed_page.locator("#boardStatus")).not_to_contain_text(
        "session-host unreachable"
    )
    expect(authed_page.locator("#boardColOther .board-count")).to_have_text("2")


_FAKE_EXCHANGE = {
    "available": True,
    "source": "native",
    "reason": None,
    "user": {"text": "please fix the merge", "timestamp": "2026-07-02T11:50:00Z"},
    "assistant": {
        "text": "Merge fixed — tests green. Ship it?",
        "timestamp": "2026-07-02T11:55:00Z",
    },
}


def _mock_exchange(
    page: Page, sid: str = "s-wait", payload: dict | None = None
) -> None:
    page.route(
        re.compile(r".*/api/board/sessions/" + sid + r"/exchange.*"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(payload or _FAKE_EXCHANGE),
        ),
    )


@pytest.mark.iphone
def test_board_card_drawer_shows_exchange_and_posts_reply(
    authed_page: Page, base_url: str
) -> None:
    """#301: tapping a session card opens the drawer with the last exchange;
    the shared composer's ➤ (#984) posts the reply body {data, submit: true}
    to the input proxy.

    Merged in #1215 — was test_board_reply_optimistically_moves_card_off_your_turn:
    #461: sending a reply relocates the card into Claude's turn right
    away — no waiting on the next poll, and no reverting back once it's
    mocked ``/api/board`` (which, unaware of the reply, still reports the
    session as needs-you) resolves."""
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
    # (#461, before the reply) the card starts in Your turn.
    expect(authed_page.locator("#boardColYours .board-count")).to_have_text("1")
    expect(authed_page.locator("#boardColClaude .board-count")).to_have_text("1")

    card = authed_page.locator(
        '.board-list[data-col="your_turn"] li.board-item'
    ).first.locator("button.board-card")
    # The header is a declared disclosure (#1259): aria-expanded follows the
    # drawer through open, close and reopen, and aria-controls names it.
    expect(card).to_have_attribute("aria-expanded", "false")
    card.click()

    drawer = authed_page.locator(".board-drawer")
    expect(drawer).to_be_visible()
    expect(card).to_have_attribute("aria-expanded", "true")
    expect(drawer).to_have_attribute("id", "board-drawer-s-wait")
    expect(card).to_have_attribute("aria-controls", "board-drawer-s-wait")
    card.click()
    expect(drawer).to_be_hidden()
    expect(card).to_have_attribute("aria-expanded", "false")
    card.click()
    expect(drawer).to_be_visible()
    expect(card).to_have_attribute("aria-expanded", "true")
    expect(drawer).to_contain_text("please fix the merge")
    expect(drawer).to_contain_text("Merge fixed — tests green. Ship it?")
    expect(drawer.locator(".board-exchange")).to_have_attribute(
        "data-state", "ready"
    )
    # The user's side of the exchange reads at 4.5:1 in both themes (#1238,
    # COLOR-02: muted text measured 4.08:1 on the dark card).
    for theme in ("light", "dark"):
        authed_page.evaluate(f"document.documentElement.dataset.theme = '{theme}'")
        ratio = stable_read(lambda: contrast_ratio(drawer.locator(".board-exchange-user")))
        assert ratio >= 4.5, f"{theme}: drawer user text at {ratio:.2f}:1, under 4.5:1"
    authed_page.evaluate("delete document.documentElement.dataset.theme")

    # The drawer stacks BELOW the card at (almost) full card width — never
    # splits it horizontally (phone feedback on #301). Raw Board geometry, so
    # read through stable_read (#680).
    box_card = stable_read(card.bounding_box)
    box_drawer = stable_read(drawer.bounding_box)
    assert box_card and box_drawer, "card/drawer not laid out"
    assert box_drawer["y"] >= box_card["y"] + box_card["height"] - 2, (
        "drawer must render below the card, not beside it"
    )
    assert box_drawer["width"] >= box_card["width"] * 0.9, (
        "drawer must span the card's width"
    )

    drawer.locator(".composer-input").fill("go ahead")
    drawer.locator(".composer-send").click()

    # -- was test_board_reply_optimistically_moves_card_off_your_turn --
    # Immediate — no fetchBoard() round trip needed to see this.
    expect(authed_page.locator("#boardColYours .board-count")).to_have_text("0")
    expect(authed_page.locator("#boardColClaude .board-count")).to_have_text("2")
    moved_card = authed_page.locator(
        '.board-list[data-col="claude_turn"] li.board-item', has_text="photo-ocr"
    )
    expect(moved_card).to_be_visible()
    expect(moved_card).to_have_class(re.compile(r"\bis-working\b"))

    # (#461, continued) Well under the 5 s poll interval, and the mocked
    # /api/board still reports needs-you for s-wait — confirms nothing
    # reverts the move. (This 1 s wait also covers the 500 ms the #301 half
    # used to give the POST to land before reading `captured` below; the
    # window after the send is kept as short as #461's own was.)
    authed_page.wait_for_timeout(1_000)
    expect(authed_page.locator("#boardColYours .board-count")).to_have_text("0")
    expect(authed_page.locator("#boardColClaude .board-count")).to_have_text("2")

    # (#301) the reply rode the input proxy as {data, submit: true}.
    assert captured.get("method") == "POST"
    assert captured.get("body") == {"data": "go ahead", "submit": True}


def test_board_codex_card_drawer_shows_agent_native_exchange(
    authed_page: Page, base_url: str
) -> None:
    """#457: a Codex card opens its own structured exchange, rather than
    degrading to the old Claude-hook-only empty message."""
    payload = _board_payload()
    payload["columns"]["claude_turn"].append({
        "session_id": "s-codex", "kind": "pty", "agent": "codex",
        "project_dir": "E:/automation/app-launcher", "name": "app-launcher",
        "alive": True, "started_at": "2026-07-02T11:57:00Z",
        "live_title": "app-launcher | fix/457", "prompt_title": "fix the drawer",
        "project": "app-launcher", "status": "unknown", "age_seconds": None,
    })
    _mock_board(authed_page, payload)
    _mock_exchange(authed_page, sid="s-codex", payload={
        "available": True, "source": "codex", "reason": None,
        "user": {"text": "fix the drawer", "timestamp": None},
        "assistant": {"text": "Codex exchange resolved.", "timestamp": None},
    })
    _open_board(authed_page, base_url)
    card = authed_page.locator(
        '.board-list[data-col="claude_turn"] li.board-item',
        has_text="app-launcher",
    ).locator("button.board-card")
    card.click()
    exchange = authed_page.locator(".board-exchange")
    expect(exchange).to_have_attribute("data-state", "ready")
    expect(exchange).to_contain_text("fix the drawer")
    expect(exchange).to_contain_text("Codex exchange resolved.")


@pytest.mark.parametrize("reason, expected_state, expected_text", [
    ("no_exchange", "empty", "No exchange yet."),
    ("capture_unparseable", "error", "Conversation preview unavailable"),
])
def test_board_drawer_distinguishes_empty_from_source_failure(
    authed_page: Page, base_url: str, reason: str, expected_state: str,
    expected_text: str,
) -> None:
    """#457: a genuinely new conversation is not described as an unlinked
    transcript, and a source failure is a distinct sanitized error state."""
    _mock_board(authed_page)
    _mock_exchange(authed_page, payload={
        "available": False, "source": None, "reason": reason,
        "user": None, "assistant": None,
    })
    _open_board(authed_page, base_url)
    authed_page.locator(
        '.board-list[data-col="your_turn"] li.board-item'
    ).first.locator("button.board-card").click()
    exchange = authed_page.locator(".board-exchange")
    expect(exchange).to_have_attribute("data-state", expected_state)
    expect(exchange).to_contain_text(expected_text)


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
    _unfold(authed_page, "boardColBacklog")
    # The dispatch bar's model selector governs one-tap starts too (#505) —
    # pick a non-default value so the POST provably carries the selection.
    authed_page.locator("#boardDispatchModel .model-combo-trigger").click()
    authed_page.locator(
        "#boardDispatchModelMenu [data-value='claude:fable']"
    ).click()
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
    assert body.get("model") == "claude:fable"


@pytest.mark.iphone
def test_backlog_issue_tile_is_flat_separator_row_with_icon_only_actions(
    authed_page: Page, base_url: str
) -> None:
    """#339: the backlog issue tile is a flat separator row (no card
    background/border), repo/# and title on their own lines, with
    icon-only ▶/⚡ actions (no "Start"/"YOLO" text) vertically centered."""
    _mock_apps_with_app_launcher(authed_page)
    _mock_board(authed_page)
    _open_board(authed_page, base_url)
    _unfold(authed_page, "boardColBacklog")

    tile = authed_page.locator('.board-list[data-col="backlog"] li.board-item').first
    expect(tile).to_be_visible(timeout=15_000)
    expect(tile).to_have_class(re.compile(r"\bboard-item-issue\b"))
    expect(tile.locator(".board-card-meta-inline")).to_have_text("app-launcher #301")
    expect(tile.locator(".board-card-title-compact")).to_have_text(
        "Board tab 2/3: drill-down + reply"
    )

    # No card chrome left on the <li> itself — that's where .app-item's
    # shared background/border/radius box actually lives (every other tab's
    # tile uses it), so clearing only the button's own chrome isn't enough;
    # a prior build regressed exactly this way.
    # to_have_css (not a raw getComputedStyle read) because it re-resolves the
    # locator on every retry: the 5 s board poll rebuilds this <li>, and a raw
    # read landing mid-rebuild returns '' on WebKit (#680).
    zero_radius = re.compile(r"^0(px)?$")
    expect(tile).to_have_css("border-radius", zero_radius)
    expect(tile).to_have_css("border-top-style", "none")
    expect(tile.locator(".board-card-flat")).to_have_css("border-radius", zero_radius)

    # Same /api/apps-population race as test_backlog_start_button_posts_issue_start
    # above: the ▶/⚡ actions only render once state.apps has landed, which can
    # trail the first render on a loaded CI runner — give it a full poll cycle.
    actions = tile.locator(".board-issue-btn")
    expect(actions.first).to_be_visible(timeout=15_000)
    expect(actions).to_have_count(2)
    expect(actions.nth(0)).to_have_attribute("aria-label", re.compile(r"^Start issue"))
    expect(actions.nth(1)).to_have_attribute("aria-label", re.compile(r"^YOLO issue"))

    # Capture both rectangles in one browser task: separate locator reads can
    # straddle the Board's 5 s replaceChildren() poll (#868).
    boxes = stable_read(
        lambda: tile.evaluate(
            "el => {"
            " const action = el.querySelector('.board-issue-btn');"
            " if (!action) return null;"
            " const tileBox = el.getBoundingClientRect();"
            " const actionBox = action.getBoundingClientRect();"
            " return {tile: {y: tileBox.y, height: tileBox.height},"
            " action: {y: actionBox.y, height: actionBox.height}};"
            "}"
        )
    )
    assert boxes is not None
    tile_box = boxes["tile"]
    action_box = boxes["action"]
    tile_center = tile_box["y"] + tile_box["height"] / 2
    action_center = action_box["y"] + action_box["height"] / 2
    assert abs(action_center - tile_center) <= 1, (
        f"issue action is not vertically centered: {action_center} vs {tile_center}"
    )


@pytest.mark.iphone
def test_backlog_issue_in_progress_is_tinted_and_actions_disabled(
    authed_page: Page, base_url: str
) -> None:
    """#528: the shared active-issue marker makes an in-flight backlog row
    visibly distinct and prevents both duplicate launch paths."""
    payload = _board_payload()
    payload["columns"]["backlog"][0]["in_progress"] = True
    payload["columns"]["backlog"].append({
        "kind": "issue", "repo": "app-launcher", "number": 302,
        "title": "A normal backlog issue", "url": "https://example.test/302",
        "updated_at": "2026-07-01T11:00:00Z", "labels": ["enhancement"],
        "in_progress": False,
    })
    _mock_apps_with_app_launcher(authed_page)
    _mock_board(authed_page, payload)
    _open_board(authed_page, base_url)
    _unfold(authed_page, "boardColBacklog")

    active = authed_page.locator(
        '.board-list[data-col="backlog"] li.board-item', has_text="#301"
    )
    normal = authed_page.locator(
        '.board-list[data-col="backlog"] li.board-item', has_text="#302"
    )
    expect(active).to_be_visible(timeout=15_000)
    expect(active).to_have_class(re.compile(r"\bis-in-progress\b"))
    expect(active.locator(".board-card-meta-inline")).to_contain_text("in progress")

    active_actions = active.locator(".board-issue-btn")
    normal_actions = normal.locator(".board-issue-btn")
    expect(active_actions).to_have_count(2)
    expect(normal_actions).to_have_count(2)
    assert all(active_actions.nth(i).is_disabled() for i in range(2))
    assert all(normal_actions.nth(i).is_enabled() for i in range(2))

    active_bg = active.evaluate("el => getComputedStyle(el).backgroundColor")
    normal_bg = normal.evaluate("el => getComputedStyle(el).backgroundColor")
    assert active_bg != normal_bg, (
        f"active backlog row must have a distinct tint: {active_bg!r} == {normal_bg!r}"
    )


def test_backlog_issue_claim_states_stale_and_unverified(
    authed_page: Page, base_url: str
) -> None:
    """#948: a claim whose owner lane is provably gone reads as a stale claim
    and stays startable; an unverifiable owner keeps the in-progress lock but
    says so."""
    payload = _board_payload()
    payload["columns"]["backlog"][0].update(in_progress=False, claim_state="dead")
    payload["columns"]["backlog"].append({
        "kind": "issue", "repo": "app-launcher", "number": 302,
        "title": "An owner-less legacy claim", "url": "https://example.test/302",
        "updated_at": "2026-07-01T11:00:00Z", "labels": ["enhancement"],
        "in_progress": True, "claim_state": "unknown",
    })
    _mock_apps_with_app_launcher(authed_page)
    _mock_board(authed_page, payload)
    _open_board(authed_page, base_url)
    _unfold(authed_page, "boardColBacklog")

    stale = authed_page.locator(
        '.board-list[data-col="backlog"] li.board-item', has_text="#301"
    )
    unverified = authed_page.locator(
        '.board-list[data-col="backlog"] li.board-item', has_text="#302"
    )
    expect(stale).to_be_visible(timeout=15_000)
    expect(stale.locator(".board-card-meta-inline")).to_contain_text("stale claim")
    expect(stale).not_to_have_class(re.compile(r"\bis-in-progress\b"))
    expect(stale.locator(".board-issue-btn").first).to_be_enabled()

    expect(unverified.locator(".board-card-meta-inline")).to_contain_text(
        "in progress (unverified)"
    )
    expect(unverified).to_have_class(re.compile(r"\bis-in-progress\b"))
    expect(unverified.locator(".board-issue-btn").first).to_be_disabled()


@pytest.mark.iphone
def test_backlog_issue_tile_wraps_a_long_title_and_grows(
    authed_page: Page, base_url: str
) -> None:
    """#862 regression guard: a long title wraps and grows the backlog row
    instead of being clipped with an ellipsis on a phone-width viewport.
    #1198: it leads the row, above its repo/# line, and wraps to the shared
    two-line cap at most, with the full text in its title attribute."""
    authed_page.set_viewport_size({"width": 430, "height": 739})
    long_title = (
        "This is a deliberately very long issue title meant to overflow the "
        "available card width so the wrapping behavior is actually exercised"
    )
    payload = copy.deepcopy(_FAKE_BOARD)
    payload["columns"]["backlog"] = [{
        "kind": "issue", "repo": "app-launcher", "number": 999,
        "title": long_title,
        "url": "https://github.com/ferraroroberto/app-launcher/issues/999",
        "updated_at": "2026-07-01T10:00:00Z", "labels": [],
    }]
    _mock_apps_with_app_launcher(authed_page)
    _mock_board(authed_page, payload)
    _open_board(authed_page, base_url)
    _unfold(authed_page, "boardColBacklog")

    tile = authed_page.locator('.board-list[data-col="backlog"] li.board-item').first
    title_el = tile.locator(".board-card-title-compact")
    expect(title_el).to_be_visible(timeout=15_000)
    expect(title_el).to_have_attribute("title", long_title)
    expect(tile.locator(".board-card-text > :first-child")).to_have_class(
        re.compile(r"\bboard-card-title-compact\b")
    )

    # A wrapped title fits inside its box: scrollWidth must not exceed
    # clientWidth. Read the two widths (not the comparison) so a mid-rebuild
    # read is recognisable as the 0/0 artifact it is rather than a silent
    # False (#680).
    widths = stable_read(
        lambda: title_el.evaluate(
            "el => el.scrollWidth && el.clientWidth"
            " ? [el.scrollWidth, el.clientWidth] : null"
        )
    )
    assert widths is not None, "title box never reported non-zero widths"
    assert widths[0] <= widths[1], (
        "title box still overflows horizontally instead of wrapping "
        f"(scrollWidth={widths[0]}, clientWidth={widths[1]})"
    )

    # Keep the three related measurements on one DOM generation; individually
    # valid reads can still straddle the Board's 5 s rebuild (#868).
    boxes = stable_read(
        lambda: tile.evaluate(
            "el => {"
            " const title = el.querySelector('.board-card-title-compact');"
            " const action = el.querySelector('.board-issue-btn');"
            " if (!title || !action) return null;"
            " const titleBox = title.getBoundingClientRect();"
            " const tileBox = el.getBoundingClientRect();"
            " const actionBox = action.getBoundingClientRect();"
            " const cs = getComputedStyle(title);"
            " const lineHeight = parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.2;"
            " return {title: {height: titleBox.height, lineHeight: lineHeight},"
            " tile: {y: tileBox.y, height: tileBox.height},"
            " action: {y: actionBox.y, height: actionBox.height}};"
            "}"
        )
    )
    assert boxes is not None
    title_box = boxes["title"]
    tile_box = boxes["tile"]
    action_box = boxes["action"]
    assert title_box["height"] > 26, (
        f"title is only {title_box['height']}px tall — it did not wrap"
    )
    assert title_box["height"] <= 2 * title_box["lineHeight"] + 1, (
        f"title is {title_box['height']}px tall — over the two-line cap "
        f"({title_box['lineHeight']}px lines)"
    )
    assert tile_box["height"] > 60, (
        f"tile is only {tile_box['height']}px tall — it did not grow with the title"
    )
    tile_center = tile_box["y"] + tile_box["height"] / 2
    action_center = action_box["y"] + action_box["height"] / 2
    assert abs(action_center - tile_center) <= 1, (
        f"issue action is not vertically centered: {action_center} vs {tile_center}"
    )


@pytest.mark.parametrize("state_sid", [
    pytest.param(None, id="session-id"),
    pytest.param("t-uuid-wait", id="state-sid"),
])
def test_board_deep_link_opens_drawer(
    authed_page: Page, base_url: str, state_sid: str | None
) -> None:
    """#301: ?board=<sid> lands on the Board with that card's drawer open —
    the target of the Slack-ping deep link. #307: a Slack ping's ?board=<sid>
    carries the hook's transcript UUID, not the card's session_id — resolve it
    via the card's state_sid instead, and expand the drawer keyed by the card's
    real session_id."""
    payload = _board_payload()
    if state_sid:
        payload["columns"]["your_turn"][0]["state_sid"] = state_sid
    _mock_board(authed_page, payload)
    _mock_exchange(authed_page)  # keyed by the real session_id, s-wait

    link_sid = state_sid or "s-wait"
    authed_page.goto(f"{base_url}/?board={link_sid}", wait_until="domcontentloaded")
    expect(authed_page.locator("#paneBoard")).to_be_visible(timeout=10_000)
    drawer = authed_page.locator(".board-drawer")
    expect(drawer).to_be_visible(timeout=10_000)
    expect(drawer).to_contain_text("Merge fixed — tests green. Ship it?")


def _mock_apps_with_app_launcher(page: Page) -> None:
    """state.apps with one claude-code entry, so the dispatch repo combobox
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


def test_dispatch_repo_dropdown_is_tap_only_and_filters_board_columns(
    authed_page: Page, base_url: str
) -> None:
    """#337: the project selector is a tap-to-select dropdown (no typing —
    a <button> trigger, not a text field), defaults to "All projects" (every
    card visible), and picking a specific project filters every kanban
    column down to that project's cards (job cards, which carry no
    repo/project, drop out of any specific-project filter). #399: Your turn
    and Other are now separate single-purpose columns.

    Then, last, the dispatch POST itself (#302 — merged in #1215 from
    test_dispatch_bar_posts_repo_mode_goal_and_keeps_text)."""
    authed_page.route(
        re.compile(r".*/api/apps$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"scan_root": "", "apps": [
                {"id": "cc-app-launcher", "kind": "claude-code",
                 "name": "app-launcher", "project_dir": "E:/automation/app-launcher"},
                {"id": "cc-voice", "kind": "claude-code",
                 "name": "voice-transcriber", "project_dir": "E:/automation/voice-transcriber"},
                {"id": "cc-life-os", "kind": "claude-code",
                 "name": "life-os", "project_dir": "E:/automation/life-os"},
                {"id": "cc-photo", "kind": "claude-code",
                 "name": "photo-ocr", "project_dir": "E:/automation/photo-ocr"},
            ]}),
        ),
    )
    _mock_board(authed_page)
    _open_board(authed_page, base_url)

    repo_btn = authed_page.locator("#boardDispatchRepoBtn")
    combo_list = authed_page.locator("#boardDispatchRepoList")

    # No editable text field exists at all — it's a real <button>.
    expect(authed_page.locator("#boardDispatchRepoInput")).to_have_count(0)
    assert repo_btn.evaluate("el => el.tagName") == "BUTTON"

    # Default: "All projects" — every _FAKE_BOARD card visible, unfiltered.
    expect(repo_btn).to_have_text("All projects", timeout=15_000)
    expect(authed_page.locator("#boardDispatchRepo")).to_have_value("")
    expect(authed_page.locator("#boardColBacklog .board-count")).to_have_text("1")
    expect(authed_page.locator("#boardColClaude .board-count")).to_have_text("1")
    expect(authed_page.locator("#boardColYours .board-count")).to_have_text("1")
    expect(authed_page.locator("#boardColOther .board-count")).to_have_text("2")
    expect(authed_page.locator("#boardColDone .board-count")).to_have_text("1")

    repo_btn.click()
    expect(combo_list).to_be_visible()
    # Same /api/apps-population race noted elsewhere in this file: the list
    # is rendered fresh on open from whatever _repoNames holds at that
    # instant, and the board's 5 s poll is what re-renders it once apps
    # land if that lagged the click — give it a full poll-cycle budget.
    expect(combo_list.locator("li[data-repo]")).to_have_count(5, timeout=15_000)  # "All" + 4 projects

    combo_list.locator('li[data-repo="app-launcher"]').click()
    expect(combo_list).to_be_hidden()
    expect(repo_btn).to_have_text("app-launcher")
    expect(authed_page.locator("#boardDispatchRepo")).to_have_value("app-launcher")

    # Filtered to app-launcher: backlog issue (repo=app-launcher) stays;
    # Claude's turn (life-os session) and Done (voice-transcriber issue) empty
    # out; Your turn drops the photo-ocr session (no match); Other keeps only
    # the app-launcher PR, dropping the job card (no project at all).
    expect(authed_page.locator("#boardColBacklog .board-count")).to_have_text("1")
    expect(authed_page.locator("#boardColClaude .board-count")).to_have_text("0")
    expect(authed_page.locator("#boardColYours .board-count")).to_have_text("0")
    expect(authed_page.locator("#boardColOther .board-count")).to_have_text("1")
    expect(authed_page.locator("#boardColDone .board-count")).to_have_text("0")
    other = authed_page.locator('.board-list[data-col="other"] li.board-item')
    expect(other).to_have_count(1)
    expect(other.first).to_contain_text("PR #158")

    # Picking "All projects" again restores every column.
    repo_btn.click()
    combo_list.locator('li[data-repo=""]').click()
    expect(repo_btn).to_have_text("All projects")
    expect(authed_page.locator("#boardColYours .board-count")).to_have_text("1")
    expect(authed_page.locator("#boardColOther .board-count")).to_have_text("2")

    # -- was test_dispatch_bar_posts_repo_mode_goal_and_keeps_text (last: it
    # POSTs a dispatch) --
    # #302: goal + repo + mode ride POST /api/board/dispatch; the goal text
    # survives the send (populated-but-clearable for rapid multi-dispatch).
    # Same app-launcher entry that test's _mock_apps_with_app_launcher served,
    # among the four projects mocked above.
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

    # The repo dropdown fills once boot's /api/apps fetch lands; the board
    # render re-syncs it, so a full poll cycle is the worst case. It defaults
    # to "All projects" (empty target), so the send needs an explicit pick.
    expect(authed_page.locator("#boardDispatchRepoBtn")).to_be_visible(timeout=15_000)
    authed_page.locator("#boardDispatchRepoBtn").click()
    authed_page.locator('#boardDispatchRepoList li[data-repo="app-launcher"]').click()
    expect(
        authed_page.locator('#boardDispatchRepo')
    ).to_have_value("app-launcher")
    expect(authed_page.locator("#boardDispatchRepoBtn")).to_have_text("app-launcher")

    authed_page.locator("#boardDispatchGoal").fill("ship the goal bar")
    # Mode is the shared .model-combo since #869 — trigger + listbox, not a
    # native <select>.
    authed_page.locator("#boardDispatchMode .model-combo-trigger").click()
    authed_page.locator("#boardDispatchModeMenu [data-value='yolo']").click()
    expect(authed_page.locator("#boardDispatchMode")).to_have_attribute(
        "data-value", "yolo"
    )
    # Model selector (#500): defaults to Sonnet; pick a non-default value so
    # the POST provably carries the selection, not a hardcoded default.
    expect(authed_page.locator("#boardDispatchModel")).to_have_attribute(
        "data-value", "claude:sonnet"
    )
    authed_page.locator("#boardDispatchModel .model-combo-trigger").click()
    authed_page.locator(
        "#boardDispatchModelMenu [data-value='codex:gpt-5.6-sol']"
    ).click()
    authed_page.locator("#boardDispatchSend").click()
    authed_page.wait_for_timeout(500)

    assert captured.get("method") == "POST"
    body = captured.get("body") or {}
    assert body.get("repo") == "app-launcher"
    assert body.get("goal") == "ship the goal bar"
    assert body.get("mode") == "yolo"
    assert body.get("model") == "codex:gpt-5.6-sol"
    # #374: a phone (non-desktop) dispatch carries the PTY spawn size so a
    # streaming agent's first output is authored at the width the overlay
    # will fit() to; a desktop client sends the mirror flag instead.
    if body.get("desktop"):
        assert "rows" not in body and "cols" not in body
    else:
        assert body.get("rows", 0) >= 10 and body.get("cols", 0) >= 20
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
    MediaRecorder present), the 🎤 shows on the dispatch bar and, enabled,
    in the drawer's shared composer (#984)."""
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
    expect(drawer.locator(".composer-mic")).to_be_visible()
    expect(drawer.locator(".composer-mic")).to_be_enabled()


@pytest.mark.iphone
def test_board_drawer_four_equal_actions_terminal_last_and_stop_kills_session(
    authed_page: Page, base_url: str, browser_name: str
) -> None:
    """#984 (mockup screen 8): under the shared composer the drawer lays out
    one row of four equal buttons — Rename · Stop · Chat · Terminal, each a
    glyph over its label at the 44px floor, Terminal last (#496 round 2) —
    and Stop kills the session via the unified stop path (#253: POST
    .../stop {mode: 'quit'}), closing the drawer."""
    _mock_board(authed_page)
    _mock_exchange(authed_page)

    captured: dict = {}

    def _capture_stop(route):
        captured["method"] = route.request.method
        captured["body"] = route.request.post_data_json
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"ok": True, "stopped": "s-wait"}),
        )

    authed_page.route(
        re.compile(r".*/api/claude-code/sessions/s-wait/stop$"), _capture_stop
    )

    _open_board(authed_page, base_url)
    authed_page.locator(
        '.board-list[data-col="your_turn"] li.board-item'
    ).first.locator("button.board-card").click()
    drawer = authed_page.locator(".board-drawer")
    expect(drawer).to_be_visible()

    composer = drawer.locator(".board-drawer-composer")
    expect(composer.locator(".composer-input")).to_be_visible()
    actions = drawer.locator(".board-drawer-actions")
    buttons = actions.locator(":scope > button")
    expect(buttons).to_have_count(4)
    order = ["board-rename-btn", "board-stop-btn", "board-open-chat", "board-open-terminal"]
    labels = ["Rename", "Stop", "Chat", "Terminal"]
    for i, (cls, label) in enumerate(zip(order, labels)):
        expect(buttons.nth(i)).to_have_class(re.compile(r"\b" + cls + r"\b"))
        expect(buttons.nth(i)).to_have_text(label)
        # A full-control Claude session can take every action.
        expect(buttons.nth(i)).not_to_have_attribute("aria-disabled", "true")
    expect(buttons.nth(0)).to_have_attribute("aria-label", "Rename this session")

    # The composer sits above the action row, and the four buttons are equal
    # columns of one row at the 44px floor. Raw geometry on a Board node goes
    # through stable_read (#680).
    box_composer = stable_read(composer.bounding_box)
    box_actions = stable_read(actions.bounding_box)
    assert box_composer and box_actions, "drawer not laid out"
    assert box_actions["y"] >= box_composer["y"] + box_composer["height"] - 2, (
        "the action row must sit below the composer"
    )
    boxes = [stable_read(buttons.nth(i).bounding_box) for i in range(4)]
    assert all(boxes), f"drawer buttons not laid out: {boxes}"
    widths = [b["width"] for b in boxes]
    assert max(widths) - min(widths) <= 1, f"drawer buttons not equal width: {widths}"
    assert len({round(b["y"]) for b in boxes}) == 1, f"drawer buttons not one row: {boxes}"
    for box in boxes:
        assert box["height"] >= 44 and box["width"] >= 44, (
            f"drawer button under the 44px floor: {box}"
        )
    assert box_actions["x"] + box_actions["width"] >= boxes[3]["x"] + boxes[3]["width"] - 1, (
        "the action row must stay inside the drawer"
    )

    # #1174: the one row is the wide-drawer layout, not a promise at every
    # width. A desktop column narrows the drawer to ~90px at 700px, where
    # four columns came to 16px each; below the room for four 44px buttons
    # the actions fold to 2x2, then to one column — never under the floor.
    if browser_name == "chromium":
        for width in (1100, 700):
            authed_page.set_viewport_size({"width": width, "height": 900})
            expect(drawer).to_be_visible()
            for i in range(4):
                box = stable_read(buttons.nth(i).bounding_box)
                assert box and box["width"] >= 44 and box["height"] >= 44, (
                    f"drawer button {labels[i]} under the 44px floor at {width}px: {box}"
                )

    buttons.nth(1).click()
    authed_page.wait_for_timeout(500)
    assert captured.get("method") == "POST"
    assert captured.get("body") == {"mode": "quit"}
    # The drawer closes (boardExpanded cleared + re-render).
    expect(drawer).to_be_hidden()


def _detached_board_payload() -> dict:
    payload = _board_payload()
    payload["columns"]["your_turn"] = [{
        "session_id": "s-remote", "kind": "remote", "agent": "claude",
        "project_dir": "E:/automation/whatsapp-radar", "name": "whatsapp-radar",
        "alive": True, "started_at": "2026-07-02T11:30:00Z",
        "live_title": "detached console", "prompt_title": "",
        "project": "whatsapp-radar", "status": "awaiting-input", "age_seconds": 660,
    }]
    return payload


def test_board_drawer_detached_session_sends_through_the_shared_composer(
    authed_page: Page, base_url: str
) -> None:
    """#984: a detached (remote) card gets the same composer as a full-control
    one — before, the drawer offered no reply at all. ➤ Send posts to the
    kind-agnostic /input route and the toast keeps the detached "Sent, not
    confirmed" wording (sessions.js::sendOutcome). ⌨ keys are disabled with
    the way to get them; Terminal stays in the row, aria-disabled, and a tap
    toasts why; Stop and Chat stay available."""
    _mock_board(authed_page, _detached_board_payload())
    _mock_exchange(authed_page, sid="s-remote")

    captured: dict = {}

    def _capture_input(route):
        captured["body"] = route.request.post_data_json
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"ok": True, "delivered": "unconfirmed"}),
        )

    authed_page.route(
        re.compile(r".*/api/claude-code/sessions/s-remote/input$"), _capture_input
    )

    _open_board(authed_page, base_url)
    authed_page.locator(
        '.board-list[data-col="your_turn"] li.board-item'
    ).first.locator("button.board-card").click()
    drawer = authed_page.locator(".board-drawer")
    expect(drawer).to_be_visible()

    composer = drawer.locator(".board-drawer-composer")
    keys = composer.locator(".composer-keys")
    expect(keys).to_be_disabled()
    expect(keys).to_have_attribute("title", "Open the terminal for keys")

    terminal = drawer.locator(".board-open-terminal")
    expect(terminal).to_have_attribute("aria-disabled", "true")
    expect(drawer.locator(".board-stop-btn")).not_to_have_attribute("aria-disabled", "true")
    expect(drawer.locator(".board-open-chat")).not_to_have_attribute("aria-disabled", "true")
    # aria-disabled keeps the tap reachable (it toasts the reason), but
    # Playwright treats it as not enabled, hence force (#982's gotcha).
    terminal.click(force=True)
    expect(authed_page.locator("#toast")).to_contain_text("Detached session — no terminal")
    expect(authed_page.locator("#terminalOverlay")).to_be_hidden()

    composer.locator(".composer-input").fill("carry on")
    composer.locator(".composer-send").click()
    expect(authed_page.locator("#toast")).to_contain_text(
        "Sent, not confirmed: typed into the PC console"
    )
    assert captured.get("body") == {"data": "carry on", "submit": True}
    expect(drawer).to_be_hidden()


def test_board_drawer_chat_opens_chat_mode_for_the_same_session(
    authed_page: Page, base_url: str
) -> None:
    """#984: the drawer's Chat button opens the session overlay in Chat mode
    (#982) for the card's own session, closing the drawer."""
    _mock_board(authed_page)
    _mock_exchange(authed_page)
    transcript_calls: list = []

    def _transcript(route):
        transcript_calls.append(route.request.url)
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "available": True, "source": "native", "reason": None,
                "session_id": "s-wait", "next_cursor": None,
                "entries": [
                    {"kind": "assistant", "timestamp": "2026-07-02T11:59:00Z",
                     "text": "Merge fixed", "truncated": False, "sidechain": False},
                ],
            }),
        )

    authed_page.route(
        re.compile(r".*/api/claude-code/sessions/s-wait/transcript(\?.*)?$"), _transcript
    )

    _open_board(authed_page, base_url)
    authed_page.locator(
        '.board-list[data-col="your_turn"] li.board-item'
    ).first.locator("button.board-card").click()
    drawer = authed_page.locator(".board-drawer")
    expect(drawer).to_be_visible()
    drawer.locator(".board-open-chat").click()

    overlay = authed_page.locator("#terminalOverlay")
    expect(overlay).to_be_visible()
    expect(overlay).to_have_attribute("data-mode", "chat")
    expect(authed_page.locator("#transcriptList")).to_contain_text("Merge fixed")
    assert transcript_calls, "Chat mode never read the session's transcript"
    assert all("/sessions/s-wait/" in u for u in transcript_calls)


def test_backlog_cards_color_coded_from_shared_git_cache(
    authed_page: Page, base_url: str
) -> None:
    """#496 item 4: a backlog card whose repo is dirty shows the red meta
    annotation, fed from the boot-time /api/claude-code/git-status cache —
    the route is mocked BEFORE navigation, and no board-poll git work is
    involved (the board payload itself carries no git fields)."""
    _mock_apps_with_app_launcher(authed_page)
    _mock_board(authed_page)
    authed_page.route(
        "**/api/claude-code/git-status",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"projects": [{
                "id": "cc-app-launcher", "is_git": True,
                "branch": "feat/496-wip", "default_branch": "main",
                "on_default_branch": False, "dirty": True,
            }]}),
        ),
    )

    _open_board(authed_page, base_url)
    _unfold(authed_page, "boardColBacklog")

    meta = authed_page.locator(
        '.board-list[data-col="backlog"] .board-card-meta-inline'
    ).first
    expect(meta).to_be_visible(timeout=15_000)
    # Red (dirty) wins over yellow when the repo is both dirty and off-main.
    expect(meta).to_have_class(re.compile(r"\bgit-dirty\b"), timeout=15_000)


def test_board_drawer_survives_git_status_poll_mid_interaction(
    authed_page: Page, base_url: str
) -> None:
    """#512: refreshGitStatus() (apps-coding.js) re-renders the Board, and
    that must not rebuild an open drawer out from under an in-progress
    interaction — renderBoard() keeps the open drawer's node (#958), the
    same path the Board's own poll takes. Reproduced with the #510
    diagnostic technique:
    tag the rename button's live DOM node, hold the boot-time
    /api/claude-code/git-status fetch open until after the drawer is open
    and tagged, then release it and confirm the tagged node (and the
    drawer) survive untouched — held-open, not a fixed sleep, per the
    convention in test_voice_dictation.py (a sleep can't reliably outlast
    Playwright's own route-dispatch latency)."""
    _mock_board(authed_page)
    _mock_exchange(authed_page)

    held_git_status: dict = {}
    authed_page.route(
        re.compile(r".*/api/claude-code/git-status$"),
        lambda route: held_git_status.__setitem__("route", route),
    )

    _open_board(authed_page, base_url)
    authed_page.locator(
        '.board-list[data-col="your_turn"] li.board-item'
    ).first.locator("button.board-card").click()

    drawer = authed_page.locator(".board-drawer")
    expect(drawer).to_be_visible()
    rename = drawer.locator(".board-rename-btn")
    expect(rename).to_be_visible()
    # The shared composer (#984) lives inside the kept node: a draft typed
    # into it must survive the re-render too.
    draft = drawer.locator(".board-drawer-composer .composer-input")
    draft.fill("half-typed reply")

    # Tag the live DOM node — if renderBoard() rebuilds the drawer, the tag
    # is lost even though the drawer stays open (boardExpanded is preserved
    # either way).
    rename.evaluate("el => { el.dataset.e2eTag = 'pre-poll'; }")

    for _ in range(100):
        if "route" in held_git_status:
            break
        authed_page.wait_for_timeout(50)
    assert "route" in held_git_status, "boot-time git-status fetch never fired"
    held_git_status["route"].fulfill(
        status=200, content_type="application/json",
        body=_json.dumps({"projects": []}),
    )
    authed_page.wait_for_timeout(400)

    expect(drawer).to_be_visible()
    expect(rename).to_have_attribute("data-e2e-tag", "pre-poll")
    expect(draft).to_have_value("half-typed reply")
    # The kept drawer gets a fresh header (#958); it still declares the
    # disclosure open (#1259).
    header = authed_page.locator("li.board-item.expanded > button.board-card")
    expect(header).to_have_attribute("aria-expanded", "true")
    expect(header).to_have_attribute("aria-controls", "board-drawer-s-wait")


@pytest.mark.iphone
def test_dispatch_bar_is_compact_and_mode_is_a_combo(
    authed_page: Page, base_url: str
) -> None:
    """#869 — the three dispatch-bar edges, in one shape check.

    (1) The goal input lives *inside* the control row rather than as its own
    full-width block above it, so the phone bar is 3 rows, not 4, and ➤ docks
    right after ✕ instead of being flung across the row by a `margin-left:
    auto`. (2) Mode is the shared `.model-combo`, the same control family as
    the model picker beside it — no native `<select>` left in the row.
    (3) ↻ is projection-dependent: docked into the dispatch row on the desktop
    grid, beside the project filter on the phone (#1198).
    (4) The project filter leads the desktop line at the far left, and drops
    below the controls on the phone so it sits just above the columns.

    (0) Merged in #1215 — was test_dispatch_model_picker_matches_shared_button_shape:
    #496/#851: Board uses the shared picker at both projections. Checked
    first, against the bar as it boots (🎤 still hidden).
    """
    _mock_board(authed_page)
    _open_board(authed_page, base_url)

    # (0) the model picker is the shared button shape. Raw Board geometry and
    # computed style, so read through stable_read (#680).
    picker = authed_page.locator("#boardDispatchModel .model-combo-trigger")
    clear = authed_page.locator("#boardDispatchClear")
    expect(picker).to_be_visible()
    box_select = stable_read(picker.bounding_box)
    box_clear = stable_read(clear.bounding_box)
    assert box_select and box_clear, "dispatch row not laid out"
    assert abs(box_select["height"] - box_clear["height"]) <= 1, (
        f"model select height {box_select['height']} != sibling button "
        f"height {box_clear['height']}"
    )
    radius = stable_read(
        lambda: picker.evaluate("el => getComputedStyle(el).borderRadius")
    )
    assert radius == "12px", f"model picker radius {radius!r} != 12px"
    assert picker.evaluate("el => el.tagName") == "BUTTON"

    # The disposable e2e webapp reports no dictation, so 🎤 stays hidden and
    # the row is one control narrower than on a real phone — where that extra
    # 36px is exactly what used to wrap ➤ onto a second line. Un-hide it so
    # every geometry assertion below runs against the real-device control
    # count, not the lucky one.
    authed_page.locator("#boardDispatchRecord").evaluate("el => { el.hidden = false; }")
    expect(authed_page.locator("#boardDispatchRecord")).to_be_visible()

    # (1) goal folded into the control row.
    expect(
        authed_page.locator(".board-dispatch-row #boardDispatchGoal")
    ).to_have_count(1)

    # (2) mode is a combo, not a <select>; picking one applies it.
    expect(authed_page.locator(".board-dispatch-row select")).to_have_count(0)
    expect(
        authed_page.locator("#boardDispatchMode.model-combo .model-combo-trigger")
    ).to_be_visible()
    authed_page.locator("#boardDispatchMode .model-combo-trigger").click()
    authed_page.locator("#boardDispatchModeMenu [data-value='build']").click()
    expect(authed_page.locator("#boardDispatchMode")).to_have_attribute(
        "data-value", "build"
    )
    expect(
        authed_page.locator("#boardDispatchMode .model-combo-trigger")
    ).to_have_text("Build")

    # (1b) ➤ sits immediately after ✕ — the old `margin-left: auto` pushed it
    # to the row's far edge with a wide gap between the two.
    clear_box = stable_read(
        lambda: authed_page.locator("#boardDispatchClear").bounding_box()
    )
    send_box = stable_read(
        lambda: authed_page.locator("#boardDispatchSend").bounding_box()
    )
    assert clear_box and send_box, "dispatch buttons not laid out"
    gap = send_box["x"] - (clear_box["x"] + clear_box["width"])
    assert 0 <= gap < 24, f"➤ should dock right after ✕, gap was {gap}px"
    # ...and on the SAME line as ✕, at every width. The row wraps only if some
    # item claims a base width it doesn't need; the goal grows from 0 instead.
    assert abs(send_box["y"] - clear_box["y"]) < 8, (
        f"➤ wrapped off ✕'s line: send y={send_box['y']}, clear y={clear_box['y']}"
    )

    # (3) + (4) ↻ home and the filter's place both depend on the projection.
    expect(authed_page.locator("#boardRefresh")).to_be_visible()
    filter_box = stable_read(
        lambda: authed_page.locator("#boardDispatchRepoBtn").bounding_box()
    )
    mode_box = stable_read(
        lambda: authed_page.locator("#boardDispatchMode").bounding_box()
    )
    refresh_box = stable_read(
        lambda: authed_page.locator("#boardRefresh").bounding_box()
    )
    assert filter_box and mode_box and refresh_box, "dispatch bar not laid out"

    viewport = authed_page.viewport_size or {"width": 0}
    if viewport["width"] < 700:
        # ↻ docks beside the project filter, the Dispatch card's last row,
        # now the column strip is gone (#1198).
        expect(authed_page.locator(".board-filter-row > #boardRefresh")).to_have_count(1)
        # The count pill keeps AA on its --card-2 fill: --fg, not --muted
        # (4.08:1 in dark, #1175).
        fg = authed_page.evaluate("getComputedStyle(document.body).color")
        expect(authed_page.locator("#boardColBacklog .board-count")).to_have_css("color", fg)
        # Filter drops BELOW the control row, so it sits just above the columns.
        assert filter_box["y"] > mode_box["y"], (
            "phone filter should stack under the controls: "
            f"filter y={filter_box['y']}, mode y={mode_box['y']}"
        )
    else:
        expect(
            authed_page.locator(".board-dispatch-row #boardRefresh")
        ).to_have_count(1)
        # One line: filter at the far left, ↻ last, everything on the same row.
        assert filter_box["x"] < mode_box["x"], (
            "desktop filter should lead the line: "
            f"filter x={filter_box['x']}, mode x={mode_box['x']}"
        )
        assert refresh_box["x"] > mode_box["x"], "↻ should trail the controls"
        for name, box in (("mode", mode_box), ("refresh", refresh_box)):
            assert abs(box["y"] - filter_box["y"]) < 8, (
                f"desktop {name} should share the filter's line: "
                f"{name} y={box['y']}, filter y={filter_box['y']}"
            )


# The shortest drawer a live session can produce: the reader found one side of
# the exchange only (a session prompted but not yet answered, or one whose user
# turn it could not recover). The `.board-exchange` block is then a single line
# instead of two, which is what lifts the composer — and the image menu above
# it — highest against the top of the column carousel.
_ONE_SIDED_EXCHANGE = {
    "available": True,
    "source": "native",
    "reason": None,
    "user": {"text": "fix the merge", "timestamp": "2026-07-02T11:50:00Z"},
    "assistant": None,
}


def _status_with_ocr(route) -> None:
    """photo-ocr configured, so the image button opens its two-row menu
    instead of going straight to the file picker (#980)."""
    resp = route.fetch()
    body = resp.json()
    body["screenshot_ocr"] = True
    route.fulfill(response=resp, json=body)


@pytest.mark.iphone
def test_drawer_image_menu_is_not_clipped_above_a_top_of_column_card(
    authed_page: Page, base_url: str
) -> None:
    """#996: the shared composer's image menu floats *above* the composer
    (`.composer-menu`), and #984 mounted that composer inside the Board
    drawer — which lives in the column carousel, whose `overflow-x: auto`
    forces `overflow-y: auto` on the same box. For the **top** card of a
    column the carousel's top edge is the card's own top edge, so with the
    shortest drawer the menu's upper edge landed 2-3px above it and was
    painted away (measured on the iPhone projection; the desktop grid drops
    the horizontal overflow, so it never clipped there).

    The fix floats the menu in viewport coordinates while it is open, which
    no ancestor's overflow clips. Asserted the way the user sees it — the
    menu's own top edge is painted — and paired with the placement it must
    keep, so the desktop projection pins that the fix moved nothing.
    """
    authed_page.route(re.compile(r".*/api/status$"), _status_with_ocr)
    _mock_board(authed_page)
    _mock_exchange(authed_page, payload=_ONE_SIDED_EXCHANGE)
    _open_board(authed_page, base_url)

    authed_page.locator(
        '.board-list[data-col="your_turn"] li.board-item'
    ).first.locator("button.board-card").click()
    drawer = authed_page.locator(".board-drawer")
    expect(drawer).to_be_visible()
    # The OCR option (and so the menu) appears only once /api/status lands,
    # which it may do after the drawer is built.
    authed_page.wait_for_function(
        "() => { const b = document.querySelector('.board-drawer .composer-image');"
        " return !!b && b.classList.contains('has-options'); }",
        timeout=15_000,
    )
    drawer.locator(".composer-image").click()
    menu = drawer.locator(".composer-menu")
    expect(menu).to_be_visible()

    measured = stable_read(lambda: authed_page.evaluate(
        """() => {
          const menu = document.querySelector('.board-drawer .composer-menu');
          const composer = document.querySelector('.board-drawer .composer');
          if (!menu || !composer) return null;
          const m = menu.getBoundingClientRect();
          const c = composer.getBoundingClientRect();
          // Hit-test the menu's own top edge: a clipped menu is not painted
          // there, and elementFromPoint answers with whatever is behind it.
          const hit = document.elementFromPoint(m.left + m.width / 2, m.top + 1);
          return {
            paintedAtOwnTop: !!hit && menu.contains(hit),
            behind: hit ? (hit.className || hit.tagName) : null,
            topInViewport: m.top >= 0,
            // Placement the CSS asks for: sitting just above the composer,
            // right-aligned inside it.
            sitsAboveComposer: m.bottom <= c.top,
            gapAboveComposer: c.top - m.bottom,
            rightInset: c.right - m.right,
          };
        }"""
    ))
    assert measured, "drawer composer menu not laid out"
    assert measured["paintedAtOwnTop"], (
        "the image menu's top edge is clipped by the column carousel "
        f"(painted there instead: {measured['behind']})"
    )
    assert measured["topInViewport"], "the image menu runs off the top of the screen"
    assert measured["sitsAboveComposer"], (
        f"the image menu must stay above the composer: {measured}"
    )
    assert 0 <= measured["gapAboveComposer"] <= 12, (
        f"the image menu drifted away from the composer: {measured}"
    )
    assert 0 <= measured["rightInset"] <= 20, (
        f"the image menu is no longer right-aligned in the composer: {measured}"
    )
