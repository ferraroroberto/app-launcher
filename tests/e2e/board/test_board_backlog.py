"""Board tab e2e: the Backlog tiles, one-tap start and the dispatch bar's repo filter."""

from __future__ import annotations

import copy
import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import _fake_clock
from tests.e2e.fixtures.ui import open_row_menu
from tests.e2e.fixtures.waits import (
    assert_unpainted_at_rest,
    boot_retrying_static_failure,
    stable_eval,
    stylesheet_report,
    wait_until,
)

from tests.e2e.board._support import _FAKE_BOARD, _board_payload, _mock_board, _open_board, _unfold


pytestmark = pytest.mark.smoke


def test_backlog_start_button_posts_issue_start(
    authed_page: Page, base_url: str
) -> None:
    """#301: a backlog card of a repo present in the projects folder carries
    ▶ Start, which posts the server-validated {repo, number, mode}."""
    # ▶ only renders for repos the Coding tab could launch in — mock
    # /api/apps so 'app-launcher' (the fake issue's repo) qualifies.
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

    start_url = re.compile(r".*/api/board/issues/start$")
    authed_page.route(
        start_url,
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "launched": "/issue-start 301", "repo": "app-launcher",
                "session": {"session_id": "sX", "kind": "pty",
                            "name": "app-launcher"},
            }),
        ),
    )

    _open_board(authed_page, base_url)
    _unfold(authed_page, "boardColBacklog")
    # The Backlog's "Start with" model (#505, moved into the lane by #1436)
    # governs one-tap starts — pick a non-default value so the POST provably
    # carries the selection.
    # The menu's real options arrive with boot's /api/config: until then it
    # holds only the static Sonnet entry, and the options render that follows
    # closes an open menu (#1346). So wait for that option to exist before
    # opening the menu — on a loaded box the config lands after the tab is up.
    fable = authed_page.locator("#boardDispatchModelMenu [data-value='claude:fable']")
    expect(fable).to_be_attached()
    authed_page.locator("#boardDispatchModel .model-combo-trigger").click()
    fable.click()
    expect(
        authed_page.locator("#boardColBacklog .board-lane-toolbar #boardDispatchModel")
    ).to_have_count(1)
    expect(authed_page.locator(".board-lane-toolbar-label")).to_have_text("Start with")
    start_btn = authed_page.locator(
        '.board-list[data-col="backlog"] .board-issue-start'
    ).first
    # The buttons render only once boot's /api/apps fetch has populated
    # state.apps; on a slow runner the first board render can precede it
    # (seen on CI). The 5 s poll re-renders with apps loaded, so a budget
    # spanning a full poll cycle makes this deterministic.
    expect(start_btn).to_be_visible(timeout=15_000)
    # Wait for the POST itself, not a fixed 500 ms a loaded box can outlast.
    with authed_page.expect_request(start_url) as start_request:
        start_btn.click()

    body = start_request.value.post_data_json or {}
    assert body.get("repo") == "app-launcher"
    assert body.get("number") == 301
    assert body.get("mode") == "start"
    assert body.get("model") == "claude:fable"


@pytest.mark.iphone
def test_backlog_issue_tile_is_flat_row_with_one_verb_and_yolo_in_kebab(
    authed_page: Page, base_url: str
) -> None:
    """#339 / #1436 / #1502: the backlog issue tile is a flat action-row (no
    card background/border), the title over "repo #N", with one trailing plain
    verb, ▶ Start, vertically centred, and a kebab holding YOLO (start and ship),
    Open on GitHub and Copy link (decision 3 of #1432). YOLO from the menu
    posts mode=yolo."""
    _mock_apps_with_app_launcher(authed_page)
    _mock_board(authed_page)
    start_url = re.compile(r".*/api/board/issues/start$")
    authed_page.route(
        start_url,
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"launched": "/issue-yolo 301", "repo": "app-launcher",
                              "session": {"session_id": "sY", "kind": "remote",
                                          "name": "app-launcher"}}),
        ),
    )
    _open_board(authed_page, base_url)
    _unfold(authed_page, "boardColBacklog")

    tile = authed_page.locator('.board-list[data-col="backlog"] li.board-item').first
    expect(tile).to_be_visible(timeout=15_000)
    expect(tile).to_have_class(re.compile(r"\bboard-item-issue\b"))
    expect(tile.locator(".action-row-meta")).to_have_text(re.compile(r"^app-launcher #301 · \S"))
    expect(tile.locator(".action-row-title")).to_have_text(
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
    expect(tile).to_have_class(re.compile(r"\baction-row\b"))

    # Same /api/apps-population race as test_backlog_start_button_posts_issue_start
    # above: ▶ only renders once state.apps has landed, which can trail the
    # first render on a loaded CI runner — give it a full poll cycle.
    start = tile.locator(".board-issue-start")
    expect(start).to_be_visible(timeout=15_000)
    expect(start).to_have_attribute("aria-label", "Start issue #301")
    # The vendored plain verb (fleet-config#1420): a glyph on nothing, never
    # the tint fill, in light and dark.
    expect(start).to_have_class(re.compile(r"\bicon-button\b"))
    expect(start).to_have_class(re.compile(r"\baction-row-verb\b"))
    expect(start).not_to_have_class(re.compile(r"\bbutton-tint\b"))
    assert_unpainted_at_rest(authed_page, start)
    expect(tile.locator(".board-issue-btn")).to_have_count(1)
    # No git tint on the meta any more (#1436): git lives on Code › Projects.
    expect(tile.locator(".action-row-meta")).not_to_have_class(
        re.compile(r"\bgit-(dirty|off-main)\b"))

    kebab = tile.locator(".board-issue-kebab")
    expect(kebab).to_have_attribute("aria-label", "Issue #301 actions")
    kebab.click()
    menu = open_row_menu(authed_page)
    expect(menu).to_be_visible()
    expect(menu.locator(".row-menu-item .row-menu-label")).to_have_text(
        ["YOLO (start and ship)", "Open on GitHub", "Copy link"])
    yolo = menu.locator("[data-item=board-issue-yolo]")
    expect(yolo).to_be_enabled()
    with authed_page.expect_request(start_url) as start_request:
        yolo.click()
    body = start_request.value.post_data_json or {}
    assert body.get("mode") == "yolo", body
    assert body.get("number") == 301, body
    expect(menu).to_be_hidden()

    # Capture both rectangles in one browser task: separate locator reads can
    # straddle a Board re-render (#868), and on the live node only (#1346).
    boxes = stable_eval(
        tile,
        "el => {"
        " const action = el.querySelector('.board-issue-start');"
        " if (!action) return null;"
        " const tileBox = el.getBoundingClientRect();"
        " const actionBox = action.getBoundingClientRect();"
        " return {tile: {y: tileBox.y, height: tileBox.height},"
        " action: {y: actionBox.y, height: actionBox.height}};"
        "}",
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
def test_backlog_issue_in_progress_shows_a_chip_and_blocks_both_starts(
    authed_page: Page, base_url: str
) -> None:
    """#528 / #1436: the shared active-issue marker makes an in-flight backlog
    row visibly distinct — an accent "in progress" chip, not a row tint
    (chips are for exceptions; a tint is a nested surface) — and prevents
    both duplicate launch paths: ▶ steps aside and the kebab's YOLO is
    disabled."""
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
    # The issue actions key on the registered projects, and the project list
    # can land after the Board drew (#1258): /api/agents, which it is
    # chained on, is held until the Board is up, and git-status, whose
    # redraw used to cover this by coming last, never answers.
    held: dict = {}
    authed_page.route(re.compile(r".*/api/agents$"), lambda route: held.__setitem__("route", route))
    authed_page.route(re.compile(r".*/api/claude-code/git-status$"), lambda route: None)
    _open_board(authed_page, base_url)
    _unfold(authed_page, "boardColBacklog")

    active = authed_page.locator(
        '.board-list[data-col="backlog"] li.board-item', has_text="#301"
    )
    normal = authed_page.locator(
        '.board-list[data-col="backlog"] li.board-item', has_text="#302"
    )
    expect(active).to_be_visible(timeout=15_000)
    for _ in range(100):
        if "route" in held:
            break
        authed_page.wait_for_timeout(50)
    assert "route" in held, "boot never fetched /api/agents"
    held["route"].continue_()
    expect(active).to_have_class(re.compile(r"\bis-in-progress\b"))
    claim = active.locator(".board-claim-chip")
    expect(claim).to_have_text("in progress")
    expect(claim).to_have_attribute("data-tone", "accent")
    expect(normal.locator(".board-claim-chip")).to_have_count(0)

    expect(normal.locator(".board-issue-start")).to_be_enabled()
    expect(active.locator(".board-issue-start")).to_have_count(0)
    active.locator(".board-issue-kebab").click()
    expect(open_row_menu(authed_page).locator("[data-item=board-issue-yolo]")).to_be_disabled()
    authed_page.keyboard.press("Escape")
    normal.locator(".board-issue-kebab").click()
    expect(open_row_menu(authed_page).locator("[data-item=board-issue-yolo]")).to_be_enabled()
    authed_page.keyboard.press("Escape")

    # No row tint: the chip carries the state (re-resolving, #680).
    expect(active).to_have_css("background-color", "rgba(0, 0, 0, 0)")


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
    expect(stale.locator(".board-claim-chip")).to_have_text("stale claim")
    expect(stale.locator(".board-claim-chip")).to_have_attribute("data-tone", "neutral")
    expect(stale).not_to_have_class(re.compile(r"\bis-in-progress\b"))
    expect(stale.locator(".board-issue-start")).to_be_enabled(timeout=15_000)

    expect(unverified.locator(".board-claim-chip")).to_have_text("in progress (unverified)")
    expect(unverified).to_have_class(re.compile(r"\bis-in-progress\b"))
    expect(unverified.locator(".board-issue-start")).to_have_count(0)


@pytest.mark.iphone
@pytest.mark.parametrize("width", [390, 1280], ids=["phone", "desktop"])
def test_backlog_issue_tile_keeps_a_long_title_on_one_line(
    authed_page: Page, base_url: str, width: int
) -> None:
    """#1502 (decision 3 of #1500, reversing #339/#862's two-line wrap): a
    long Backlog title stays on one line with an ellipsis on every width, the
    full text in its title attribute; its repo reference ellipsizes inside
    the meta line rather than pushing past it (LAYOUT-08), and the ▶ verb
    stays centred against the one-line row."""
    authed_page.set_viewport_size({"width": width, "height": 900})
    long_title = (
        "This is a deliberately very long issue title meant to overflow the "
        "available card width so the ellipsis is actually exercised"
    )
    payload = copy.deepcopy(_FAKE_BOARD)
    payload["columns"]["backlog"] = [{
        "kind": "issue", "repo": "a-repository-name-" + "far-too-long-" * 12 + "for-any-meta-line",
        "number": 999, "title": long_title,
        "url": "https://github.com/ferraroroberto/app-launcher/issues/999",
        "updated_at": "2026-07-01T10:00:00Z", "labels": [],
        "in_progress": True,
    }]
    _mock_apps_with_app_launcher(authed_page)
    _mock_board(authed_page, payload)
    # #1609: the title is `nowrap` only through the vendored action-row.css. A
    # sheet that fails to load under gate load leaves the page built but
    # unstyled for good (`white-space: normal`), and no wait recovers it, so a
    # failed static load gets one fresh boot and a second one fails by name.
    tile = authed_page.locator('.board-list[data-col="backlog"] li.board-item').first
    title_el = tile.locator(".action-row-title")

    def boot() -> None:
        _open_board(authed_page, base_url)
        _unfold(authed_page, "boardColBacklog")
        expect(title_el).to_be_visible(timeout=15_000)

    boot_retrying_static_failure(authed_page, boot)
    expect(title_el).to_have_attribute("title", long_title)
    try:
        expect(title_el).to_have_css("white-space", "nowrap")
    except AssertionError as exc:
        raise AssertionError(
            f"{exc}\nstylesheets (rules per sheet): {stylesheet_report(authed_page)}"
        ) from None
    expect(title_el).to_have_css("text-overflow", "ellipsis")

    # One generation of the row (#868, #1346): the title is one line and
    # clipped, the meta text clipped inside its line with the chip whole, and
    # nothing in the row reaches past the row's right edge.
    m = stable_eval(
        tile,
        "el => {"
        " const title = el.querySelector('.action-row-title');"
        " const meta = el.querySelector('.action-row-meta');"
        " const text = el.querySelector('.action-row-meta-text');"
        " const chip = el.querySelector('.board-claim-chip');"
        " if (!title || !meta || !text || !chip || !title.clientWidth) return null;"
        " const cs = getComputedStyle(title);"
        " const row = el.getBoundingClientRect();"
        " return {titleH: title.getBoundingClientRect().height,"
        " lineH: parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.5,"
        " titleClipped: title.scrollWidth > title.clientWidth,"
        " textClipped: text.scrollWidth > text.clientWidth,"
        " metaRight: meta.getBoundingClientRect().right,"
        " chipRight: chip.getBoundingClientRect().right,"
        " rowRight: row.right};"
        "}",
    )
    assert m is not None, "the row never measured"
    assert m["titleH"] <= m["lineH"] + 1, f"title wraps: {m}"
    assert m["titleClipped"], f"a long title should ellipsize: {m}"
    assert m["textClipped"], f"a long repo reference should ellipsize: {m}"
    assert m["chipRight"] <= m["metaRight"] + 0.5, f"the chip spills off the meta line: {m}"
    assert m["metaRight"] <= m["rowRight"] + 0.5, f"the meta line overflows the row: {m}"


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
    and Other (PRs and jobs since #1436) are separate single-purpose columns.
    #1436: the filter lives in the lane toolbar; the header stays fleet-wide.

    Then, last, what is left of the old dispatch bar (#1382 — the free-text
    dispatch POST this used to end on is gone; #1436 — so is the chief
    composer)."""
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
    # The header is the fleet's, not the filtered view's.
    expect(authed_page.locator("#boardHeadStatus")).to_have_text("1 failing · 1 needs you")

    # Picking "All projects" again restores every column.
    repo_btn.click()
    combo_list.locator('li[data-repo=""]').click()
    expect(repo_btn).to_have_text("All projects")
    expect(authed_page.locator("#boardColYours .board-count")).to_have_text("1")
    expect(authed_page.locator("#boardColOther .board-count")).to_have_text("2")

    # -- was test_dispatch_bar_posts_repo_mode_goal_and_keeps_text --
    # #1382: the free-text Add / Build / Yolo dispatch is gone, and #1436
    # took the chief's message box off the Board. There is no mode control,
    # and the model combo that stays is the Backlog's "Start with" — its pick
    # riding a one-tap start is pinned by the issue-start test above.
    expect(authed_page.locator("#boardDispatchMode, #boardDispatchGoal")).to_have_count(0)
    expect(authed_page.locator("#boardDispatchModel")).to_have_attribute(
        "data-value", "claude:sonnet"
    )
    expect(authed_page.locator("#boardDispatchModel .model-combo-trigger")).to_have_attribute(
        "aria-label", "Start with model"
    )


@pytest.mark.iphone
def test_backlog_meta_carries_no_git_tint(
    authed_page: Page, base_url: str
) -> None:
    """#1436 (replacing #496 item 4's red/yellow annotation): a backlog row
    whose repo is dirty and off its default branch still draws its meta in
    the plain muted colour — git lives on Code › Projects. The git-status
    answer lands first, then a full Board poll renders over it, so a tint
    fed from that cache would have had its chance to appear."""
    _mock_apps_with_app_launcher(authed_page)
    _mock_board(authed_page)
    served: dict = {}

    def _git_status(route):
        served["git"] = True
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"projects": [{
                "id": "cc-app-launcher", "is_git": True,
                "branch": "feat/496-wip", "default_branch": "main",
                "on_default_branch": False, "dirty": True,
            }]}),
        )

    authed_page.route("**/api/claude-code/git-status", _git_status)
    _fake_clock.install(authed_page)
    _open_board(authed_page, base_url)
    _unfold(authed_page, "boardColBacklog")

    meta = authed_page.locator(
        '.board-list[data-col="backlog"] .action-row-meta'
    ).first
    expect(meta).to_be_visible(timeout=15_000)
    wait_until(authed_page, lambda: served.get("git"), "boot fetching git-status")
    with authed_page.expect_response(lambda resp: resp.url.endswith("/api/board")):
        _fake_clock.jump_interval(authed_page, 5_000, "fetchBoard")
    muted = authed_page.evaluate(
        "() => { const el = document.createElement('span');"
        " el.style.color = 'var(--muted)'; document.body.appendChild(el);"
        " const c = getComputedStyle(el).color; el.remove(); return c; }"
    )
    expect(meta).not_to_have_class(re.compile(r"\bgit-(dirty|off-main)\b"))
    expect(meta).to_have_css("color", muted)
