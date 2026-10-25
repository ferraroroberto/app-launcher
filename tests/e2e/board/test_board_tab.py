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
tests/unit/board/ + tests/test_board_dispatch.py.

The rest of the Board's browser coverage (drawer, backlog, lane layout) lives in
``tests/e2e/board/``; the shared mocks are in ``tests/e2e/board/_support.py``."""

from __future__ import annotations

import copy
import json as _json
import re
from datetime import datetime, timezone

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import _fake_clock
from tests.e2e._contrast import contrast_ratio
from tests.e2e._surface import surface
from tests.e2e.fixtures.ui import open_row_menu
from tests.e2e.fixtures.waits import flush_requests, stable_eval, stable_read, wait_until

from tests.e2e.board._support import (
    _FAKE_BOARD,
    _board_payload,
    _iso_utc,
    _mock_board,
    _mock_exchange,
    _open_board,
    _plan,
    _route_board_from,
    _route_plan_from,
    _unfold,
)


pytestmark = pytest.mark.smoke


def _assert_board_structure(authed_page: Page) -> None:
    """Check the board's toolbar, header exceptions and section cards."""
    # -- was test_board_sections_are_collapsible_cards_that_survive_the_poll
    # (structure + default open state) --
    # #1198 / #1436: the Board is built like every other tab. There is no
    # Dispatch card and no chief card (the chief lives in its row under Code
    # › Sessions), no chief composer and no usage line: the first thing under
    # the page header is the lane toolbar — the project filter and ↻ — then
    # the lanes, each a collapsible section card with its count in the
    # summary; the phone's column strip is gone. A section the user folds
    # stays folded across the 5 s poll: renderBoard() re-renders the lists,
    # never a section's open state (that half is the last step below).
    first = authed_page.locator("#paneBoard > .page-head + *")
    expect(first).to_have_class(re.compile(r"\bboard-toolbar\b"))
    expect(first.locator("#boardDispatchRepoBtn")).to_have_count(1)
    expect(first.locator("#boardRefresh")).to_have_count(1)
    expect(authed_page.locator(
        "#boardDispatchCard, #boardChiefCard, #boardDispatchGoal, #boardChiefStatus, "
        "#boardUsage, #paneBoard .usage-meter")).to_have_count(0)
    expect(authed_page.locator("#paneBoard .collapse-title", has_text="Dispatch")).to_have_count(0)

    # The header (#1436): exceptions only, in their tones, two at most; no
    # slogan. Worst tone first (#1586, the vendored renderHeadStatus): the
    # failing job (danger) leads, then the chief's unanswered question
    # (#1502); "needs you", the third exception, waits its turn.
    head = authed_page.locator("#boardHeadStatus")
    expect(head).to_have_text("1 failing · 1 question")
    expect(head.locator(".head-exception")).to_have_count(2)
    expect(head.locator(".head-exception").nth(0)).to_have_attribute("data-tone", "danger")
    expect(head.locator(".head-exception").nth(1)).to_have_attribute("data-tone", "attention")

    sections = authed_page.locator("#boardColumns details.board-col")
    expect(sections).to_have_count(5)
    # Lane order (#1436 decision log): Backlog, the chief's plan, Claude's
    # turn, Your turn, PRs and jobs (renamed from "Other"), Done today.
    expect(
        authed_page.locator("#boardColumns details > summary .collapse-title")
    ).to_have_text(["Backlog", "Chief's plan", "Claude's turn", "Your turn",
                    "PRs and jobs", "Done today"])
    expect(
        authed_page.locator("#boardColumns details.board-col > summary .board-count")
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


def _assert_counts_and_your_turn(authed_page: Page) -> None:
    """Check per-column counts, the attention mark and the Your-turn card."""
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
    # The shared session row (#1433): the avatar leads, then the title over
    # the project/status meta line; the one-line title keeps its full text
    # reachable.
    expect(yours.nth(0).locator(".board-card > :first-child")).to_have_class(
        re.compile(r"\bavatar\b")
    )
    title = yours.nth(0).locator(".board-card .action-row-text > :first-child")
    expect(title).to_have_class(re.compile(r"\bboard-card-title\b"))
    expect(title).to_have_attribute("title", "chunk merge fix")
    expect(yours.nth(0).locator(".board-card .action-row-text > :nth-child(2)")).to_have_class(
        re.compile(r"\bboard-card-meta\b")
    )
    # The synthetic design-review veto (.fleet.toml no_go) spares this class.
    expect(yours.nth(0).locator(".board-card")).to_have_class(
        re.compile(r"\bboard-card-session\b")
    )


def _assert_columns_geometry(authed_page: Page, viewport: dict) -> None:
    """Check the column sections stack on phone and sit in a grid on desktop."""
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


def _assert_chief_plan(authed_page: Page, viewport: dict) -> None:
    """Check the chief's plan card, its groups and the 320px title clipping."""
    # -- the chief's plan (#1279): a card, not a column, directly above
    # Claude's turn in both projections (#1436) --
    expect(authed_page.locator("#boardChiefPlan + #boardColClaude")).to_have_count(1)
    plan_card = authed_page.locator("#boardChiefPlan")
    expect(plan_card).to_have_class(re.compile(r"\bcard\b"))
    expect(plan_card).not_to_have_class(re.compile(r"\bboard-col\b"))
    box_claude = stable_read(authed_page.locator("#boardColClaude").bounding_box)
    box_plan = stable_read(plan_card.bounding_box)
    assert box_claude and box_plan, "Claude's turn / chief's plan not laid out"
    assert abs(box_plan["x"] - box_claude["x"]) < 2, (box_plan, box_claude)
    assert 0 <= box_claude["y"] - (box_plan["y"] + box_plan["height"]) <= 32, (
        f"Claude's turn should sit right under the plan: {box_claude} vs {box_plan}")

    plan_body = plan_card.locator(".board-plan-body")
    expect(plan_body).to_have_attribute("data-state", "ok")
    # No chief card in this board, and the session list was read: said
    # plainly, so a stale plan never reads as current.
    expect(plan_body.locator(".board-plan-chief")).to_have_text(
        "Chief not running — this plan may be out of date.")
    expect(plan_body.locator(".board-plan-age")).to_have_text("Updated 12 min ago")
    waiting = plan_body.locator(".board-plan-group-waiting")
    expect(waiting.locator(".board-plan-heading")).to_have_text("Waiting on you")
    expect(waiting.locator("li.board-plan-waiting")).to_contain_text("Remember the last tab?")
    expect(waiting.locator("li.board-plan-waiting")).to_contain_text("app-launcher#1131")
    groups = plan_body.locator(".board-plan-heading")
    expect(groups).to_have_text(["Waiting on you", "Lanes (1)", "Queue (2)"])
    lane = plan_body.locator(".board-plan-group").nth(1).locator("li.board-plan-row")
    expect(lane).to_have_count(1)
    expect(lane).to_contain_text("app-launcher")
    expect(lane).to_contain_text("#1273")
    # The tone map (#1436): a lane at the gate is work in progress, accent.
    expect(lane.locator(".board-plan-chip")).to_have_attribute("data-tone", "accent")
    queue = plan_body.locator(".board-plan-group").nth(2).locator("li.board-plan-row")
    expect(queue).to_have_count(2)
    # Title first (#1297): the repo and number follow on line 2 (#1578); the
    # row itself opens the issue, so no inline link sits inside it.
    first_title = queue.nth(0).locator(".action-row-main > *").first
    expect(first_title).to_have_class(re.compile(r"\baction-row-title\b"))
    expect(first_title).to_have_text("Chat by default on desktop")
    expect(queue.nth(0).locator("a")).to_have_count(0)
    expect(queue.nth(0).locator(".action-row-meta-text")).to_have_text("app-launcher#1273")
    expect(queue.nth(0).locator(".board-plan-chip")).to_have_text("gate")
    expect(queue.nth(0).locator(".board-plan-chip")).to_have_attribute("data-tone", "accent")
    expect(queue.nth(1).locator(".action-row-meta-text")).to_have_text(
        "automation#135 · before Thu 1 Oct 16:00")
    # An unknown status is shown as it is, on the neutral chip.
    expect(queue.nth(1).locator(".board-plan-chip")).to_have_text("someday")
    expect(queue.nth(1).locator(".board-plan-chip")).to_have_attribute("data-tone", "neutral")
    if viewport["width"] < 700:
        # At the narrowest phone a long title ellipsizes on its one line
        # (#1502) and nothing scrolls sideways (#1297).
        authed_page.set_viewport_size({"width": 320, "height": viewport["height"]})
        long_title = queue.nth(1).locator(".action-row-title")
        clipped = stable_eval(
            long_title, "el => el.scrollWidth > el.clientWidth + 1 && el.clientWidth > 0")
        assert clipped, "long queue title should ellipsize on one line at 320px"
        overflow = stable_read(lambda: authed_page.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"))
        assert overflow <= 0, f"page scrolls sideways at 320px by {overflow}px"
        authed_page.set_viewport_size(viewport)


def _assert_github_columns(authed_page: Page) -> None:
    """Unfold the GitHub-fed columns and check their cards."""
    # -- the GitHub-fed columns' cards (this test's own) --
    _unfold(authed_page, "boardColBacklog", "boardColOther", "boardColDone")

    # PRs and jobs holds the open PR + failed job, in that order, on the
    # shared anatomy (#1436): the failed job's state is a danger chip, the
    # PR's normal state no chip at all.
    other = authed_page.locator('.board-list[data-col="other"] li.board-item')
    expect(other.first).to_be_visible(timeout=5_000)
    assert other.count() == 2
    expect(other.nth(0)).to_contain_text("PR #158")
    expect(other.nth(0).locator(".chip")).to_have_count(0)
    # Both rows ride the shared action-row now (#1501, no cardShell), with
    # a one-line title.
    for i in range(2):
        expect(other.nth(i)).to_have_class(re.compile(r"\baction-row\b"))
        expect(other.nth(i).locator(".action-row-title")).to_have_css("white-space", "nowrap")
    job_chip = other.nth(1).locator(".board-job-chip")
    expect(job_chip).to_have_text("failed")
    expect(job_chip).to_have_attribute("data-tone", "danger")

    # Backlog card is repo · #N · title; done card is a closed issue.
    backlog = authed_page.locator('.board-list[data-col="backlog"] li.board-item')
    expect(backlog.first).to_contain_text("app-launcher #301")
    # A Backlog row's content starts at the same inset inside its column as a
    # Claude's-turn card's (#1303: the flat row had 4px, the cards 14px).
    # A session card leads with its avatar since #1433, so its leading edge
    # is the avatar's, not the title's.
    inset = """(li) => {
      const list = li.closest('.board-list').getBoundingClientRect();
      const btn = li.querySelector('.action-row-main');
      const lead = btn.querySelector('.avatar') || btn.querySelector('.action-row-title');
      return lead.getBoundingClientRect().left - list.left;
    }"""
    claude_card = authed_page.locator('.board-list[data-col="claude_turn"] li.board-item').first
    backlog_inset = stable_eval(backlog.first, inset)
    card_inset = stable_eval(claude_card, inset)
    assert abs(backlog_inset - card_inset) <= 1, (
        f"Backlog row text inset {backlog_inset}px vs Claude's-turn card {card_inset}px")
    # The Backlog's footer says how fresh the GitHub cache is (#1436).
    expect(authed_page.locator('.board-lane-foot[data-col="backlog"]')).to_have_text(
        re.compile(r"^GitHub · updated (just now|\d+ min ago)$"))
    done = authed_page.locator('.board-list[data-col="done"] li.board-item')
    expect(done.first).to_contain_text("#87")
    expect(done.first).to_contain_text("closed")
    expect(done.first).to_have_class(re.compile(r"\baction-row\b"))
    expect(done.first.locator(".action-row-title")).to_have_css("white-space", "nowrap")
    surface(authed_page, "board.tab")


@pytest.mark.iphone
def test_board_renders_columns_counts_and_cards(
    authed_page: Page, base_url: str, tmp_path
) -> None:
    """One render of the mocked board, checked top to bottom (#1215 merged
    the section-structure and column-geometry tests in here): the section
    cards and their default open state, counts, the Your-turn card, the
    projection's column geometry — all before anything is unfolded — then
    the GitHub-fed columns' cards, and last a fold that must survive a poll.
    """
    _mock_board(authed_page, plan=_plan(tmp_path))
    _fake_clock.install(authed_page)
    _open_board(authed_page, base_url)

    _assert_board_structure(authed_page)
    viewport = authed_page.viewport_size or {"width": 0}
    yours_section = authed_page.locator("#boardColYours")

    _assert_counts_and_your_turn(authed_page)

    _assert_columns_geometry(authed_page, viewport)

    _assert_chief_plan(authed_page, viewport)

    _assert_github_columns(authed_page)

    # -- was test_board_sections_are_collapsible_cards_that_survive_the_poll
    # (the fold that must survive a poll; last: it changes section state) --
    authed_page.locator("#boardColYours > summary").click()
    expect(yours_section).not_to_have_attribute("open", "")
    # Jump one full poll on the fake clock (#1515): its response lands, then
    # its render runs (two frames later it has).
    with authed_page.expect_response(lambda resp: resp.url.endswith("/api/board")):
        _fake_clock.jump_interval(authed_page, 5_000, "fetchBoard")
    authed_page.evaluate(
        "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
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
    # The automatic refresh is a POST fired as the board loads: wait for it,
    # or (fresh cache, none expected) for the board to have rendered, then let
    # any request still in flight land before the exact-list check.
    if expected_posts:
        wait_until(authed_page, lambda: len(posts) >= len(expected_posts),
                   "the automatic gh refresh POST")
    else:
        expect(authed_page.locator("li.board-item").first).to_be_visible()
    flush_requests(authed_page)

    assert posts == expected_posts, (
        f"gh cache aged {gh_age_seconds}s should auto-refresh {expected_posts} "
        f"on tab open, got {posts}"
    )

    # -- was test_board_refresh_button_posts_gh_refresh --
    # ↻ is disabled while a refresh is in flight (refreshGithub()), so let
    # the automatic one settle before tapping it.
    expect(authed_page.locator("#boardRefresh")).to_be_enabled()
    authed_page.locator("#boardRefresh").click()
    wait_until(authed_page, lambda: len(posts) >= len(expected_posts) + 1,
               "the refresh tap's gh refresh POST")
    flush_requests(authed_page)

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
    _fake_clock.install(authed_page)
    _open_board(authed_page, base_url)
    expect(authed_page.locator("#boardColBacklog .board-count")).to_have_text("1")

    with authed_page.expect_request(
        lambda req: req.method == "POST" and req.url.endswith("/api/board/github/refresh"),
        timeout=15_000,
    ):
        current["body"] = _unfetched_payload()
        _fake_clock.jump_interval(authed_page, 5_000, "fetchBoard")


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


def _assert_calm_header_states(authed_page: Page, head, plan_body, current: dict, read_empty: dict) -> None:
    """Walk the header from plain totals to blank plan to unknown session count."""
    # Nothing wrong: the header falls back to the plain totals (#1436).
    expect(head).to_have_text("1 failing")
    calm = copy.deepcopy(read_empty)
    calm["columns"]["other"] = [c for c in calm["columns"]["other"] if c["kind"] != "job"]
    calm["columns"]["claude_turn"] = copy.deepcopy(_FAKE_BOARD["columns"]["claude_turn"])
    # No plan file: the card stays blank, no note, no line.
    current["body"] = calm
    current["plan"] = {"state": "empty"}
    expect(authed_page.locator("#boardRefresh")).to_be_enabled()
    authed_page.locator("#boardRefresh").click()
    expect(plan_body).to_have_attribute("data-state", "empty")
    expect(plan_body.locator("> *")).to_have_count(0)
    expect(head).to_have_text("1 session · 1 issue")
    expect(head.locator(".head-exception")).to_have_count(0)
    # And with the session list unreadable, the count is said to be
    # unknown, never "0 sessions".
    blind_calm = copy.deepcopy(calm)
    blind_calm["live_sessions"] = {"available": False, "error": "session-host unreachable"}
    blind_calm["columns"]["claude_turn"] = []
    current["body"] = blind_calm
    authed_page.locator("#boardRefresh").click()
    expect(head).to_have_text("sessions unknown · 1 issue")


def test_board_unreadable_sources_render_unknown_not_zero(
    authed_page: Page, base_url: str, tmp_path
) -> None:
    """#915: an unreachable session-host must not read as "Nothing needs you
    right now." — the live columns show an unknown count, a distinct message
    and a status line; a job with unreadable history shows as a card, not as
    nothing. Once the list is read and genuinely empty, the same columns show
    a plain 0 and today's text — a real zero must not look like a failure.

    The chief's plan card (#1279) follows the same rule: with the session
    list unread, whether the chief runs is unknown, not "not running"; an
    unreadable plan is a quiet note and no toast; a missing one is blank."""
    _mock_board(authed_page)
    current = {"body": _blind_payload(), "plan": _plan(tmp_path)}
    _route_board_from(authed_page, current)
    _route_plan_from(authed_page, current)
    _open_board(authed_page, base_url)

    plan_body = authed_page.locator("#boardChiefPlan .board-plan-body")
    expect(plan_body.locator(".board-plan-chief")).to_have_text(
        "Chief status unknown — session-host unreachable.")
    expect(plan_body.locator("li.board-plan-row")).to_have_count(4)

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
    # The header shows the exceptions it knows (the chief's question, #1502,
    # and the failed job); never "0 sessions" (#1436).
    head = authed_page.locator("#boardHeadStatus")
    expect(head).to_have_text("1 failing · 1 question")
    expect(authed_page.locator("#boardColBacklog .board-count")).to_have_text("1")
    expect(authed_page.locator("#boardColOther .board-count")).to_have_text("3")
    unreadable = authed_page.locator(
        '.board-list[data-col="other"] li.board-item.is-unreadable'
    )
    expect(unreadable.locator(".action-row-meta-text")).to_have_text(re.compile(r"^job\b"))
    expect(unreadable.locator(".board-job-chip")).to_have_text("unreadable")
    expect(unreadable.locator(".board-job-chip")).to_have_attribute("data-tone", "attention")
    expect(unreadable).to_contain_text("daily digest")

    read_empty = _board_payload()
    read_empty["live_sessions"] = {"available": True, "error": None}
    read_empty["columns"]["claude_turn"] = []
    read_empty["columns"]["your_turn"] = []
    expect(authed_page.locator("#boardRefresh")).to_be_enabled()
    current["body"] = read_empty
    current["plan"] = {"state": "unreadable"}
    authed_page.locator("#boardRefresh").click()

    expect(plan_body).to_have_attribute("data-state", "unreadable")
    expect(plan_body.locator(".board-plan-note")).to_have_text("Plan unreadable.")
    expect(plan_body.locator("li.board-plan-row")).to_have_count(0)
    expect(plan_body.locator(".board-plan-chief")).to_have_count(0)
    expect(authed_page.locator("#toast")).not_to_contain_text(re.compile("plan", re.I))

    for section in ("#boardColClaude", "#boardColYours"):
        expect(authed_page.locator(f"{section} .board-count")).to_have_text("0")
    expect(yours_empty.locator(".empty-state-message")).to_have_text(
        "Nothing needs you right now.")
    expect(
        authed_page.locator('.board-empty[data-col="claude_turn"] .empty-state-message')
    ).to_have_text("No sessions on Claude’s side.")
    # Start work (#1238 J-09) goes where sessions start: the Code tab
    # (#1436: the Board has no composer any more).
    yours_empty.locator(".empty-state-action").click()
    expect(authed_page.locator("#paneClaude")).to_be_visible()
    authed_page.locator("#tabBoard").click()
    expect(authed_page.locator("#paneBoard")).to_be_visible()
    expect(authed_page.locator("#boardStatus")).not_to_contain_text(
        "session-host unreachable"
    )
    expect(authed_page.locator("#boardColOther .board-count")).to_have_text("2")

    _assert_calm_header_states(authed_page, head, plan_body, current, read_empty)


def _route_input_capture(authed_page: Page) -> dict:
    """Route the s-wait input proxy and return the dict it fills on POST."""
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
    return captured


def _assert_drawer_below_card(authed_page: Page, card, drawer) -> None:
    """Check the drawer stacks below the card at almost full card width."""
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

    captured = _route_input_capture(authed_page)

    _fake_clock.install(authed_page)
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
    surface(authed_page, "board.drawer")
    # The user's side of the exchange reads at 4.5:1 in both themes (#1238,
    # COLOR-02: muted text measured 4.08:1 on the dark card).
    for theme in ("light", "dark"):
        authed_page.evaluate(f"document.documentElement.dataset.theme = '{theme}'")
        ratio = stable_read(lambda: contrast_ratio(drawer.locator(".board-exchange-user")))
        assert ratio >= 4.5, f"{theme}: drawer user text at {ratio:.2f}:1, under 4.5:1"
    authed_page.evaluate("delete document.documentElement.dataset.theme")

    _assert_drawer_below_card(authed_page, card, drawer)

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
    wait_until(authed_page, lambda: captured.get("method") == "POST",
               "the reply POST")
    # The window the old 1 s sleep gave a revert to show up in, jumped on the
    # fake clock instead (still well under the 5 s poll).
    _fake_clock.advance(authed_page, 1_000)
    expect(authed_page.locator("#boardColYours .board-count")).to_have_text("0")
    expect(authed_page.locator("#boardColClaude .board-count")).to_have_text("2")

    # (#301) the reply rode the input proxy as {data, submit: true}.
    assert captured.get("method") == "POST"
    assert captured.get("body") == {"data": "go ahead", "submit": True}


def _route_stop_capture(authed_page: Page) -> dict:
    """Route the s-wait stop endpoint and return the dict it fills on POST."""
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
    return captured


def _assert_drawer_action_geometry(composer, actions, buttons) -> None:
    """Check the composer sits above the action row and the buttons meet the 44px floor."""
    # The composer sits above the action row; Chat and Terminal are equal,
    # side by side, at the 44px floor. Raw geometry on a Board node goes
    # through stable_read (#680).
    box_composer = stable_read(composer.bounding_box)
    box_actions = stable_read(actions.bounding_box)
    assert box_composer and box_actions, "drawer not laid out"
    assert box_actions["y"] >= box_composer["y"] + box_composer["height"] - 2, (
        "the action row must sit below the composer"
    )
    boxes = [stable_read(buttons.nth(i).bounding_box) for i in range(3)]
    assert all(boxes), f"drawer buttons not laid out: {boxes}"
    for box in boxes:
        assert box["height"] >= 44 and box["width"] >= 44, (
            f"drawer button under the 44px floor: {box}"
        )
    if box_actions["width"] >= 280:
        # Room for both labels beside the kebab: one row, Chat and Terminal
        # equal.
        assert abs(boxes[0]["width"] - boxes[1]["width"]) <= 1, f"Chat and Terminal differ: {boxes}"
        assert len({round(b["y"]) for b in boxes}) == 1, f"drawer actions not one row: {boxes}"
    else:
        # A narrow desktop column: Chat and the kebab, then Terminal across
        # the drawer.
        assert boxes[0]["y"] == boxes[2]["y"] < boxes[1]["y"], f"narrow drawer layout: {boxes}"


def _assert_drawer_floor_on_narrow_desktop(authed_page: Page, browser_name: str, drawer, actions, buttons) -> None:
    """On Chromium, check the 44px floor and unclipped labels at narrow desktop widths."""
    # #1174: never under the floor at a narrow desktop column either.
    if browser_name == "chromium":
        for width in (1100, 700):
            authed_page.set_viewport_size({"width": width, "height": 900})
            expect(drawer).to_be_visible()
            for i in range(3):
                box = stable_read(buttons.nth(i).bounding_box)
                assert box and box["width"] >= 44 and box["height"] >= 44, (
                    f"drawer action {i} under the 44px floor at {width}px: {box}"
                )
            if width < 1100:
                continue  # five lanes in 700px leave words no room; only the floor holds
            # Chat and Terminal keep their whole words in a desktop lane.
            clipped = stable_eval(actions, (
                "el => el.isConnected ? Array.from(el.querySelectorAll("
                "'.board-drawer-action-label')).filter("
                "l => l.scrollWidth > l.clientWidth + 1).map(l => l.textContent) : null"))
            assert clipped == [], f"drawer labels clipped at {width}px: {clipped}"


@pytest.mark.iphone
def test_board_drawer_chat_primary_menu_and_stop_confirms(
    authed_page: Page, base_url: str, browser_name: str
) -> None:
    """#1502 (decision 4 of #1500, was #984's four equal buttons): under the
    shared composer the drawer shows Chat, the one tint primary, with
    Terminal beside it, each a glyph beside its label at the 44px floor, then
    a kebab holding Rename and Stop. The exchange renders as markdown. Stop
    always asks, in the themed dialog: "no" keeps the session, "Stop" kills it
    via the unified stop path (#253: POST .../stop {mode: 'quit'}) and closes
    the drawer."""
    _mock_board(authed_page)
    _mock_exchange(authed_page, payload={
        "available": True, "reason": None,
        "user": {"text": "status?", "timestamp": None},
        "assistant": {"text": "Merge **fixed**:\n\n- tests green\n- ready", "timestamp": None},
    })

    captured = _route_stop_capture(authed_page)

    _open_board(authed_page, base_url)
    authed_page.locator(
        '.board-list[data-col="your_turn"] li.board-item'
    ).first.locator("button.board-card").click()
    drawer = authed_page.locator(".board-drawer")
    expect(drawer).to_be_visible()

    # Markdown, through Chat mode's renderer: real markup, no raw asterisks.
    assistant = drawer.locator(".board-exchange-assistant")
    expect(assistant.locator("strong")).to_have_text("fixed")
    expect(assistant.locator("li")).to_have_text(["tests green", "ready"])
    expect(assistant).not_to_contain_text("**")

    composer = drawer.locator(".board-drawer-composer")
    expect(composer.locator(".composer-input")).to_be_visible()
    actions = drawer.locator(".board-drawer-actions")
    buttons = actions.locator(":scope > button")
    expect(buttons).to_have_count(3)
    chat, terminal, kebab = buttons.nth(0), buttons.nth(1), buttons.nth(2)
    expect(chat).to_have_class(re.compile(r"\bboard-open-chat\b.*\bis-primary\b"))
    expect(chat).to_have_text("Chat")
    expect(terminal).to_have_class(re.compile(r"\bboard-open-terminal\b"))
    expect(terminal).to_have_text("Terminal")
    expect(terminal).not_to_have_class(re.compile(r"\bis-primary\b"))
    expect(kebab).to_have_attribute("aria-label", "Session actions")
    # A full-control Claude session can take every action.
    for b in (chat, terminal):
        expect(b).not_to_have_attribute("aria-disabled", "true")
    # Rename and Stop are not on the row: they are in the kebab's menu.
    expect(actions.locator(":scope > [data-item=board-rename-btn], :scope > [data-item=board-stop-btn]")).to_have_count(0)

    _assert_drawer_action_geometry(composer, actions, buttons)

    _assert_drawer_floor_on_narrow_desktop(authed_page, browser_name, drawer, actions, buttons)

    kebab.click()
    menu = open_row_menu(authed_page)
    expect(menu).to_be_visible()
    expect(menu.locator(".row-menu-item .row-menu-label")).to_have_text(["Rename", "Stop"])
    expect(menu.locator("[data-item=board-stop-btn]")).to_have_class(re.compile(r"\brow-menu-danger\b"))

    # Stop asks first, in the themed dialog; "no" keeps the session.
    menu.locator("[data-item=board-stop-btn]").click()
    dialog = authed_page.locator("#confirmDialog")
    expect(dialog).to_be_visible()
    expect(dialog.locator("#confirmDialogTitle")).to_have_text(re.compile(r"^Stop .+\?$"))
    dialog.locator("#confirmDialogClose").click()
    expect(dialog).to_be_hidden()
    flush_requests(authed_page)
    assert "method" not in captured, "Stop posted without a yes"
    expect(drawer).to_be_visible()

    kebab.click()
    menu.locator("[data-item=board-stop-btn]").click()
    expect(dialog).to_be_visible()
    dialog.locator("#confirmDialogOk").click()
    wait_until(authed_page, lambda: captured.get("method") == "POST",
               "the drawer's quit POST")
    assert captured.get("body") == {"mode": "quit"}
    # The drawer closes (boardExpanded cleared + re-render).
    expect(drawer).to_be_hidden()
