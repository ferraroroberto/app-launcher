"""Board tab e2e: lane toolbar, filters, card dates and empty or folded lanes."""

from __future__ import annotations

import copy
import json as _json
import re
from datetime import datetime, timedelta, timezone

import pytest
from playwright.sync_api import Page, expect

from scripts import synthetic_gh
from tests.e2e._contrast import contrast_ratio
from tests.e2e.conftest import (
    _autoboot_enabled, flush_requests,
    stable_eval, wait_until,
)

from tests.e2e.board._support import (
    _FAKE_BOARD,
    _board_payload,
    _iso_utc,
    _mock_board,
    _open_board,
    _plan,
    _unfold,
)


pytestmark = pytest.mark.smoke


@pytest.mark.iphone
def test_lane_toolbar_and_start_with_geometry(
    authed_page: Page, base_url: str
) -> None:
    """#1436 — the lane toolbar and "Start with", in one shape check.

    (1) The toolbar is the Board's first row under the page header, the first
    lane, Backlog, right under it: the project filter fills the line and ↻
    trails it, both at the 44px floor. (2) "Start with" is the shared
    `.model-combo` (#496/#851), in the Backlog lane.
    """
    _mock_board(authed_page)
    _open_board(authed_page, base_url)

    # One browser task for every related box (#1346): the lanes re-render on
    # the poll.
    boxes = stable_eval(
        authed_page.locator("#paneBoard"),
        """pane => {
          const box = sel => {
            const el = pane.querySelector(sel);
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return {x: r.x, y: r.y, w: r.width, h: r.height};
          };
          return {
            head: box('.page-head'), toolbar: box('.board-toolbar'),
            filter: box('#boardDispatchRepoBtn'), refresh: box('#boardRefresh'),
            first: box('#boardColumns > details'),
            firstId: pane.querySelector('#boardColumns > details').id,
          };
        }""",
    )
    assert boxes and all(v for v in boxes.values()), f"Board not laid out: {boxes}"
    assert boxes["toolbar"]["y"] >= boxes["head"]["y"] + boxes["head"]["h"], boxes
    assert boxes["first"]["y"] >= boxes["toolbar"]["y"] + boxes["toolbar"]["h"], boxes
    assert boxes["firstId"] == "boardColBacklog", boxes["firstId"]
    assert boxes["filter"]["h"] >= 44 - 0.5, f"filter is {boxes['filter']['h']}px tall"
    assert boxes["refresh"]["w"] >= 44 - 0.5 and boxes["refresh"]["h"] >= 44 - 0.5, boxes["refresh"]
    assert boxes["refresh"]["x"] > boxes["filter"]["x"] + boxes["filter"]["w"], boxes
    assert abs((boxes["refresh"]["y"] + boxes["refresh"]["h"] / 2)
               - (boxes["filter"]["y"] + boxes["filter"]["h"] / 2)) <= 2, boxes
    viewport = authed_page.viewport_size or {"width": 0}
    if viewport["width"] < 700:
        assert boxes["filter"]["w"] >= boxes["toolbar"]["w"] - 44 - 8 - 1, (
            f"phone filter should fill the line beside ↻: {boxes['filter']} vs {boxes['toolbar']}")
        # The count pill keeps AA on its --card-2 fill: --fg, not --muted
        # (4.08:1 in dark, #1175).
        fg = authed_page.evaluate("getComputedStyle(document.body).color")
        expect(authed_page.locator("#boardColBacklog .board-count")).to_have_css("color", fg)

    # (2) "Start with" is the shared picker shape, in the Backlog lane.
    _unfold(authed_page, "boardColBacklog")
    picker = authed_page.locator("#boardColBacklog .board-lane-toolbar .model-combo-trigger")
    expect(picker).to_be_visible()
    expect(picker).to_have_css("border-radius", "12px")
    assert picker.evaluate("el => el.tagName") == "BUTTON"
    expect(authed_page.locator("#paneBoard select")).to_have_count(0)


def test_desktop_project_filter_is_as_wide_as_the_backlog_column(
    authed_page: Page, base_url: str, browser_name: str
) -> None:
    """#1464 — on the desktop Board the project filter's left and right edges
    line up with the Backlog column's, at every wide layout (700 and 1100px
    switch the pane's padding, so both sides of each are measured). The ↻
    button stays beside it. Chromium only: the iPhone projection is a coarse
    pointer, which the desktop block never applies to."""
    if browser_name != "chromium":
        pytest.skip("desktop grid needs a fine pointer; the WebKit projection is the iPhone")
    _mock_board(authed_page)
    for width in (1280, 1600, 1920, 1100, 1099, 700):
        authed_page.set_viewport_size({"width": width, "height": 900})
        _open_board(authed_page, base_url)
        boxes = stable_eval(
            authed_page.locator("#paneBoard"),
            """pane => {
              const box = sel => {
                const r = pane.querySelector(sel).getBoundingClientRect();
                return {x: r.x, w: r.width};
              };
              return {filter: box('#boardDispatchRepoBtn'),
                      refresh: box('#boardRefresh'),
                      backlog: box('#boardColBacklog')};
            }""",
        )
        f, b, r = boxes["filter"], boxes["backlog"], boxes["refresh"]
        assert abs(f["x"] - b["x"]) <= 1, f"{width}px: filter left {f} vs Backlog {b}"
        assert abs((f["x"] + f["w"]) - (b["x"] + b["w"])) <= 1, (
            f"{width}px: filter right edge {f} vs Backlog {b}"
        )
        assert r["x"] >= f["x"] + f["w"], f"{width}px: ↻ not beside the filter: {boxes}"


@pytest.mark.iphone
def test_refresh_in_flight_spins_at_full_strength(
    authed_page: Page, base_url: str
) -> None:
    """#1502 (COLOR-02): while a GitHub refresh runs, ↻ keeps its glyph and
    colour and says it is busy (aria-busy), instead of a disabled "…" whose
    line colour measured 1.55:1 on dark; at least 3:1 in both themes. A
    second tap meanwhile sends nothing."""
    _mock_board(authed_page)
    held: list = []
    authed_page.route(re.compile(r".*/api/board/github/refresh$"), lambda route: held.append(route))
    _open_board(authed_page, base_url)

    refresh = authed_page.locator("#boardRefresh")
    refresh.click()
    wait_until(authed_page, lambda: len(held) >= 1, "the refresh POST")
    expect(refresh).to_have_attribute("aria-busy", "true")
    expect(refresh.locator("svg.icon")).to_have_count(1)
    expect(refresh).not_to_have_attribute("disabled", "")
    for theme in ("light", "dark"):
        authed_page.evaluate(f"document.documentElement.dataset.theme = '{theme}'")
        ratio = contrast_ratio(refresh)
        assert ratio >= 3, f"{theme}: refresh in flight at {ratio:.2f}:1, under 3:1"
    authed_page.evaluate("delete document.documentElement.dataset.theme")

    # aria-disabled keeps it reachable; Playwright treats it as not enabled.
    refresh.click(force=True)
    flush_requests(authed_page)
    assert len(held) == 1, "a tap during the refresh sent a second one"

    held[0].fulfill(status=200, content_type="application/json", body=_json.dumps(
        {"fetched_at": _iso_utc(datetime.now(timezone.utc)), "error": None}))
    expect(refresh).to_have_attribute("aria-busy", "false")


def test_the_disposable_webapp_refreshes_github_from_the_synthetic_gh(
    request: pytest.FixtureRequest, authed_page: Page, base_url: str
) -> None:
    """#1516: the gate never reaches the real ``gh``. Any test that opens the
    Board unmocked sets off the GitHub refresh (about 15 real refreshes per
    gate run before), so autoboot points the disposable webapp's
    ``LAUNCHER_GH_CMD`` at ``scripts/synthetic_gh.py``. A refresh the real
    ``gh`` answered would carry the owner's real repositories."""
    if not _autoboot_enabled(request.config):
        pytest.skip("live-tray run: the live webapp reads the real gh by design")
    refresh = authed_page.request.post(f"{base_url}/api/board/github/refresh")
    assert refresh.ok, refresh.status
    assert refresh.json()["error"] is None
    board = authed_page.request.get(f"{base_url}/api/board").json()
    repos = {
        card["repo"]
        for column in ("backlog", "other", "done")
        for card in board["columns"][column]
        if card.get("kind") != "job"
    }
    assert repos == {synthetic_gh.REPO}, sorted(repos)

def _local_stamp(dt: datetime, today: bool) -> str:
    """What the viewer sees for ``dt``: the clock today, ``14 Oct`` otherwise.
    The browser and this test share the machine's time zone."""
    local = dt.astimezone()
    return local.strftime("%H:%M") if today else f"{local.day} {local.strftime('%b')}"


@pytest.mark.iphone
def test_board_cards_carry_their_date_and_it_is_never_cut(
    authed_page: Page, base_url: str
) -> None:
    """#1638: a Done card reads ``<repo> #<n> · closed · <close time>``; a
    Backlog issue and a PR card show their last-updated time. Today is a clock
    time, an earlier day a short date, both in the viewer's local time. At 390px
    the date must stay whole (LAYOUT-09: an ellipsis cut it) and the repo name
    must stay first. Done is listed newest close first, even when the older
    close has the later ``updated_at`` (a comment)."""
    now = datetime.now(timezone.utc)
    earlier = now - timedelta(days=3)
    closed_old = now - timedelta(minutes=50)
    closed_new = now - timedelta(minutes=5)
    payload = _board_payload()
    cols = payload["columns"]
    cols["backlog"][0]["updated_at"] = _iso_utc(earlier)
    cols["other"][0]["updated_at"] = _iso_utc(now)
    cols["done"] = [
        {"kind": "issue", "repo": "voice-transcriber", "number": 87, "title": "newer close",
         "url": "u87", "updated_at": _iso_utc(closed_new), "closed_at": _iso_utc(closed_new),
         "state": "closed", "labels": []},
        {"kind": "issue", "repo": "voice-transcriber", "number": 86, "title": "older close, later comment",
         "url": "u86", "updated_at": _iso_utc(now), "closed_at": _iso_utc(closed_old),
         "state": "closed", "labels": []},
    ]
    _mock_board(authed_page, payload)
    authed_page.set_viewport_size({"width": 390, "height": 844})
    _open_board(authed_page, base_url)
    _unfold(authed_page, "boardColBacklog", "boardColOther", "boardColDone")

    def meta_of(col: str, index: int = 0):
        return authed_page.locator(
            f'.board-list[data-col="{col}"] li.board-item'
        ).nth(index).locator(".action-row-meta")

    expect(meta_of("backlog")).to_have_text(
        f"app-launcher #301 · {_local_stamp(earlier, today=False)}")
    expect(meta_of("other")).to_have_text(
        f"app-launcher PR #158 · {_local_stamp(now, today=True)}")
    expect(meta_of("done", 0)).to_have_text(
        f"voice-transcriber #87 · closed · {_local_stamp(closed_new, today=True)}")
    expect(meta_of("done", 1)).to_have_text(
        f"voice-transcriber #86 · closed · {_local_stamp(closed_old, today=True)}")

    # Whole, not clipped: the meta shows every line it has, and the stamp is
    # the tail of its text (so the repo name leads and nothing pushes it out).
    for col, index in (("backlog", 0), ("other", 0), ("done", 0), ("done", 1)):
        clipped = stable_eval(
            meta_of(col, index), "el => el.scrollHeight - el.clientHeight")
        assert clipped <= 1, f"{col}[{index}] meta is cut by {clipped}px"
        text = stable_eval(meta_of(col, index), "el => el.textContent")
        assert "…" not in text


@pytest.mark.iphone
def test_in_progress_backlog_card_keeps_its_date_whole_beside_a_long_repo(
    authed_page: Page, base_url: str
) -> None:
    """#1638: a claim chip makes the meta a one-line ellipsizing text, so the
    date is its own non-shrinking item: a long repo name is what gets cut."""
    when = datetime.now(timezone.utc) - timedelta(days=2)
    payload = copy.deepcopy(_FAKE_BOARD)
    payload["columns"]["backlog"] = [{
        "kind": "issue", "repo": "a-repository-name-" + "far-too-long-" * 12 + "for-any-meta-line",
        "number": 999, "title": "t", "url": "u999",
        "updated_at": _iso_utc(when), "labels": [], "in_progress": True,
    }]
    _mock_board(authed_page, payload)
    authed_page.set_viewport_size({"width": 390, "height": 844})
    _open_board(authed_page, base_url)
    _unfold(authed_page, "boardColBacklog")
    meta = authed_page.locator('.board-list[data-col="backlog"] li.board-item').first.locator(
        ".action-row-meta")
    stamp = meta.locator(".board-card-stamp")
    expect(stamp).to_have_text(f"· {_local_stamp(when, today=False)}")
    m = stable_eval(meta, """el => {
        const s = el.querySelector('.board-card-stamp');
        const t = el.querySelector('.action-row-meta-text');
        return { stampWhole: s.scrollWidth <= s.clientWidth,
                 stampRight: s.getBoundingClientRect().right,
                 metaRight: el.getBoundingClientRect().right,
                 textClipped: t.scrollWidth > t.clientWidth };
    }""")
    assert m["stampWhole"] and m["stampRight"] <= m["metaRight"] + 1, m
    assert m["textClipped"], f"the long repo should be the part that ellipsizes: {m}"


def test_desktop_empty_lane_is_a_one_line_strip(
    authed_page: Page, base_url: str, browser_name: str
) -> None:
    """#1502 — at the wide query an empty lane is a one-line strip across the
    grid (title, count, its empty sentence and its one action inline), and
    the lanes that hold rows share the width, side by side, the project
    filter still exactly one of them wide (#1464). Chromium only: the iPhone
    projection is a coarse pointer, which the desktop block never applies
    to."""
    if browser_name != "chromium":
        pytest.skip("desktop grid needs a fine pointer; the WebKit projection is the iPhone")
    payload = _board_payload()
    payload["columns"]["your_turn"] = []
    payload["columns"]["done"] = []
    _mock_board(authed_page, payload)
    authed_page.set_viewport_size({"width": 1280, "height": 900})
    _open_board(authed_page, base_url)
    expect(authed_page.locator("#boardColYours")).to_have_class(re.compile(r"\bis-empty\b"))
    expect(authed_page.locator("#boardColBacklog")).not_to_have_class(re.compile(r"\bis-empty\b"))

    boxes = stable_eval(
        authed_page.locator("#paneBoard"),
        """pane => {
          const box = el => {
            const r = el.getBoundingClientRect();
            return {x: r.x, y: r.y, w: r.width, h: r.height, cy: r.y + r.height / 2};
          };
          const q = sel => pane.querySelector(sel);
          return {
            grid: box(q('#boardColumns')), backlog: box(q('#boardColBacklog')),
            stack: box(q('.board-col-stack')), other: box(q('#boardColOther')),
            yours: box(q('#boardColYours')), done: box(q('#boardColDone')),
            title: box(q('#boardColYours .collapse-title')),
            message: box(q('#boardColYours .empty-state-message')),
            action: box(q('#boardColYours .empty-state-action')),
            filter: box(q('#boardDispatchRepoBtn')),
          };
        }""",
    )
    b, g = boxes["backlog"], boxes["grid"]
    for strip in ("yours", "done"):
        s_ = boxes[strip]
        assert s_["h"] <= 60, f"{strip} strip is {s_['h']}px tall, not one line"
        assert abs(s_["w"] - g["w"]) <= 1, f"{strip} strip does not span the grid: {s_} vs {g}"
        assert s_["y"] >= b["y"] + b["h"], f"{strip} strip sits beside a lane: {s_} vs {b}"
    for part in ("message", "action"):
        assert abs(boxes[part]["cy"] - boxes["title"]["cy"]) <= 4, (
            f"the strip's {part} is not on the title's line: {boxes}")
    # Three lanes with rows (Backlog, the plan + Claude's turn, PRs and jobs)
    # share the width in order, side by side.
    assert abs(b["y"] - boxes["stack"]["y"]) <= 1 and abs(b["y"] - boxes["other"]["y"]) <= 1
    assert b["x"] < boxes["stack"]["x"] < boxes["other"]["x"]
    assert b["w"] > g["w"] / 3 - 20, f"Backlog did not take the empty lanes' width: {b} vs {g}"
    f = boxes["filter"]
    assert abs(f["x"] - b["x"]) <= 1 and abs((f["x"] + f["w"]) - (b["x"] + b["w"])) <= 1, (
        f"filter is not one lane wide: {f} vs {b}")


def test_folded_lanes_show_their_exception_in_tone(
    authed_page: Page, base_url: str, tmp_path
) -> None:
    """#1436: a folded lane says what is wrong instead of its count — PRs and
    jobs "1 failing" in the danger tone, the chief's plan "1 question for
    you" in the attention tone — and unfolded, the lane shows its count
    again. A lane with nothing wrong keeps its count either way."""
    _mock_board(authed_page, plan=_plan(tmp_path))
    _open_board(authed_page, base_url)

    def fold(section_id: str) -> None:
        section = authed_page.locator(f"#{section_id}")
        if section.get_attribute("open") is not None:
            authed_page.locator(f"#{section_id} > summary").click()
        expect(section).not_to_have_attribute("open", "")

    other = authed_page.locator("#boardColOther")
    fold("boardColOther")
    exception = other.locator("> summary .board-lane-exception")
    expect(exception).to_be_visible()
    expect(exception).to_have_text("1 failing")
    expect(exception).to_have_attribute("data-tone", "danger")
    expect(other.locator("> summary .board-count")).to_be_hidden()

    _unfold(authed_page, "boardColOther")
    expect(exception).to_be_hidden()
    expect(other.locator("> summary .board-count")).to_have_text("2")
    expect(other.locator("> summary .board-count")).to_be_visible()

    plan = authed_page.locator("#boardChiefPlan")
    fold("boardChiefPlan")
    question = plan.locator("> summary .board-lane-exception")
    expect(question).to_be_visible()
    expect(question).to_have_text("1 question for you")
    expect(question).to_have_attribute("data-tone", "attention")

    # Nothing wrong in Done today: folded, it still shows its count.
    fold("boardColDone")
    expect(authed_page.locator("#boardColDone > summary .board-lane-exception")).to_have_count(0)
    expect(authed_page.locator("#boardColDone > summary .board-count")).to_be_visible()
