"""Telegram channel sessions leave the lists for a summary line (#1384).

A channel session (label ``telegram:<profile>``) serves a household member
through their own chat; the Board and the Coding tab's session list hide it by
default (the ``hide_channel_sessions`` setting, on). The Coding tab replaces it
with one Telegram row -- "N running" -- that opens a read-only list: status,
last activity and context use, with no Stop or delete; the Board has no such
row since #1436. Switched off, both lists behave as they always did.

Hermetic: the session list, the Board, the context route and the setting are
route-mocked, so no real session is involved. The context polls are driven with
the fake page clock (#1376): "slow while the list is closed, fast only while it
is open" is a claim about time, so the tests jump time instead of sleeping on it.

#1402 adds the context alert on the summary row at >= 50 % context and a Compact
button per popup row. ``/input`` is route-mocked: a real Telegram session is
never sent ``/compact`` from a test.
"""

from __future__ import annotations

import copy
import json
import re
import time
from datetime import datetime, timezone

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import _fake_clock
from tests.e2e.conftest import (
    close_settings_sheets,
    flush_requests,
    open_settings_sheet,
    stable_eval,
    wait_until,
)

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]


def _session(sid: str, label: str, name: str, **extra) -> dict:
    base = {
        "session_id": sid, "kind": "pty", "agent": "claude", "label": label,
        "name": name, "project_dir": f"E:/automation/{name}", "alive": True,
        "started_at": "2026-10-03T08:00:00Z", "live_title": "", "prompt_title": "",
        "manual_title": "", "flags": "", "last_output_at": time.time() - 300,
        "shared_name": None, "shared_name_source": None,
    }
    base.update(extra)
    return base


_WORKER = _session("s-work", "", "life-os", live_title="weekly recap")
_HEALTH = _session("s-tg-health", "telegram:health", "life-os")
_FAMILY = _session("s-tg-family", "telegram:family-chat", "life-os")
_SESSIONS = [_WORKER, _HEALTH, _FAMILY]


def _card(sess: dict, status: str = "working") -> dict:
    return {
        "session_id": sess["session_id"], "kind": "pty", "agent": "claude",
        "label": sess["label"], "project_dir": sess["project_dir"],
        "name": sess["name"], "alive": True, "started_at": sess["started_at"],
        "live_title": sess["live_title"], "prompt_title": "", "manual_title": "",
        "project": "life-os", "status": status, "age_seconds": 120,
    }


def _mock(page: Page, *, hidden: bool = True) -> dict:
    """Route-mock everything the lists read. Returns the live knobs."""
    knobs = {
        "hidden": hidden, "posts": [], "context_hits": [], "inputs": [],
        # session_id -> context percent; absent = "not showing" (no figure).
        "pct": {"s-tg-health": 42},
    }

    def _sessions(route):
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"sessions": copy.deepcopy(_SESSIONS)}))

    def _board(route):
        gh = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "generated_at": gh,
            "columns": {
                "backlog": [], "claude_turn": [_card(s) for s in _SESSIONS],
                "your_turn": [], "other": [], "done": [],
            },
            "github": {"fetched_at": gh, "error": None},
            "sessions_state": {"available": True, "stale": False, "updated_at": gh},
        }))

    def _config(route):
        if route.request.method == "POST":
            body = route.request.post_data_json or {}
            knobs["posts"].append(body)
            if "hide_channel_sessions" in body:
                knobs["hidden"] = body["hide_channel_sessions"]
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": True}))
            return
        resp = route.fetch()
        body = resp.json()
        body["hide_channel_sessions"] = knobs["hidden"]
        route.fulfill(response=resp, json=body)

    def _context(route):
        sid = route.request.url.split("/sessions/")[1].split("/")[0]
        knobs["context_hits"].append(sid)
        pct = knobs["pct"].get(sid)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(
            {"available": pct is not None, "percent": pct,
             "reason": None if pct is not None else "not_showing"}))

    page.route(re.compile(r".*/api/claude-code/sessions$"), _sessions)
    page.route(re.compile(r".*/api/board(?:\?.*)?$"), _board)
    page.route(re.compile(r".*/api/config$"), _config)
    page.route(re.compile(r".*/api/claude-code/sessions/[^/]+/context$"), _context)
    def _input(route):
        sid = route.request.url.split("/sessions/")[1].split("/")[0]
        knobs["inputs"].append((sid, route.request.post_data_json))
        route.fulfill(status=200, content_type="application/json", body=json.dumps(
            {"delivered": True, "submit_state": "confirmed"}))

    page.route(re.compile(r".*/api/claude-code/sessions/[^/]+/input$"), _input)
    page.route(re.compile(r".*/api/board/chief-plan$"), lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({"state": "empty"})))
    page.route(re.compile(r".*/api/claude-code/git-status$"), lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({"projects": []})))
    return knobs


def _open_coding(page: Page, base_url: str) -> None:
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_selector("#tabClaude", state="attached")
    page.locator("#tabClaude").click()
    expect(page.locator("#sessionsList")).to_be_attached()


def _open_board(page: Page, base_url: str) -> None:
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_selector("#tabBoard", state="attached")
    page.locator("#tabBoard").click()
    expect(page.locator("#paneBoard")).to_be_visible()


def test_coding_list_swaps_channel_rows_for_a_summary_line(
    authed_page: Page, base_url: str
) -> None:
    _mock(authed_page)
    _open_coding(authed_page, base_url)

    rows = authed_page.locator("#sessionsList li.session-item")
    expect(rows).to_have_count(1)
    expect(rows.first).to_have_attribute("data-session-id", "s-work")
    expect(authed_page.locator("#sessionsList .session-channel-tag")).to_have_count(0)
    # A flat row since #1434: the send-glyph avatar, "Telegram", "2 running".
    summary = authed_page.locator("#sessionsChannelSummary")
    expect(summary).to_be_visible()
    expect(summary).to_have_class(re.compile(r"\bchannel-row\b"))
    expect(summary.locator(".srow-title")).to_have_text("Telegram")
    expect(summary.locator(".channel-summary-text")).to_have_text("2 running")
    box = summary.bounding_box()
    assert box and box["height"] >= 44, f"summary line is not a 44px target: {box}"


def test_board_drops_channel_cards_and_has_no_telegram_row(
    authed_page: Page, base_url: str
) -> None:
    """#1436 (decision 4 of #1432): the Board leaves Telegram sessions out of
    its lanes and carries no Telegram row of its own; the Coding tab's row
    still says how many run."""
    _mock(authed_page)
    _open_board(authed_page, base_url)

    cards = authed_page.locator("#boardColumns li.board-item")
    expect(cards).to_have_count(1)
    expect(authed_page.locator("#boardColumns .session-channel-tag")).to_have_count(0)
    expect(authed_page.locator("#boardChannelSummary")).to_have_count(0)
    expect(authed_page.locator("#paneBoard .channel-row, #paneBoard .channel-summary")).to_have_count(0)
    expect(authed_page.locator("#paneBoard")).not_to_contain_text("Telegram")
    expect(authed_page.locator("#sessionsChannelSummary .channel-summary-text")).to_have_text(
        "2 running")


def test_summary_opens_a_read_only_list_and_polls_context_fast_only_while_open(
    authed_page: Page, base_url: str
) -> None:
    _fake_clock.install(authed_page)
    knobs = _mock(authed_page)
    _open_coding(authed_page, base_url)
    _fake_clock.wait_for_interval(authed_page, 5000)   # boot has armed its polls

    # The summary line reads each running session once on its own (#1402),
    # then only at the slow cadence: 30 s later nothing has been asked again.
    wait_until(authed_page, lambda: len(knobs["context_hits"]) >= 2,
               "the summary line's first context reads")
    flush_requests(authed_page)
    first = len(knobs["context_hits"])
    _fake_clock.advance(authed_page, 30_000)
    flush_requests(authed_page)
    assert len(knobs["context_hits"]) == first, "context polled fast with the list closed"

    authed_page.locator("#sessionsChannelSummary").click()
    dialog = authed_page.locator("#channelListDialog")
    expect(dialog).to_be_visible()
    rows = dialog.locator("li.channel-list-row")
    expect(rows).to_have_count(2)
    health = dialog.locator('li[data-session-id="s-tg-health"]')
    expect(health).to_contain_text("Telegram · Health")
    expect(health).to_contain_text("Running")
    # last_output_at is five minutes old when the mock is built; the page
    # clock is jumped before this, so assert the shape, not the minute.
    expect(health).to_contain_text(re.compile(r"\d+m ago"))
    wait_until(authed_page, lambda: "s-tg-health" in knobs["context_hits"],
               "the open list's first context read")
    expect(health.locator(".channel-list-context")).to_have_text("42%")
    # A session with no figure on screen is "—", never 0%.
    expect(dialog.locator('li[data-session-id="s-tg-family"] .channel-list-context')
           ).to_have_text("—")

    # Read-only by construction: the only controls are the row opens and the
    # two ways to close — no Stop, no delete, no kill anywhere in it.
    labels = dialog.locator("button").all_inner_texts()
    assert not [t for t in labels if re.search(r"stop|delete|kill|remove", t, re.I)], labels
    expect(dialog.locator(".action-stop-close, .board-stop-btn")).to_have_count(0)

    # Polls while open…
    before = len(knobs["context_hits"])
    _fake_clock.advance(authed_page, 10_000)
    flush_requests(authed_page)
    assert len(knobs["context_hits"]) > before, "the open list never re-polled context"

    # …and drops back to the slow cadence the moment it closes: 10 s later
    # nothing is asked; a minute later each running session is read again.
    authed_page.locator("#channelListDone").click()
    expect(dialog).to_be_hidden()
    flush_requests(authed_page)
    closed_at = len(knobs["context_hits"])
    _fake_clock.advance(authed_page, 10_000)
    flush_requests(authed_page)
    assert len(knobs["context_hits"]) == closed_at, "context polled fast after the list closed"
    _fake_clock.advance(authed_page, 60_000)
    flush_requests(authed_page)
    # Two sessions, so one slow tick is 2 reads; the fast cadence would be 12.
    assert closed_at < len(knobs["context_hits"]) <= closed_at + 4, \
        "the slow summary poll is not one read per session per minute"


def test_a_hidden_channel_session_opens_for_a_look_but_cannot_be_stopped(
    authed_page: Page, base_url: str
) -> None:
    _mock(authed_page)
    _open_coding(authed_page, base_url)
    authed_page.locator("#sessionsChannelSummary").click()
    authed_page.locator(
        '#channelListDialog li[data-session-id="s-tg-health"] .channel-list-open').click()
    authed_page.wait_for_selector("#terminalOverlay:not([hidden])")
    expect(authed_page.locator("#channelListDialog")).to_be_hidden()

    authed_page.locator("#terminalMenu").click()
    menu = authed_page.locator("#terminalOverlay .row-menu")
    expect(menu.first).to_be_visible()
    # The destructive item is not in the menu at all while the session is hidden.
    expect(menu.locator(".action-stop-close")).to_have_count(0)
    expect(menu.locator("button", has_text="Stop")).to_have_count(0)


def test_switching_the_setting_off_restores_rows_cards_and_stop(
    authed_page: Page, base_url: str
) -> None:
    knobs = _mock(authed_page)
    _open_coding(authed_page, base_url)
    expect(authed_page.locator("#sessionsList li.session-item")).to_have_count(1)

    open_settings_sheet(authed_page, "channelsSheet")
    toggle = authed_page.locator("#hideChannelSessionsToggle")
    expect(toggle).to_have_attribute("aria-checked", "true")
    toggle.click()
    wait_until(authed_page, lambda: {"hide_channel_sessions": False} in knobs["posts"],
               "the setting's POST")
    expect(toggle).to_have_attribute("aria-checked", "false")

    # An open sheet makes the nav inert: close it, then back on the Coding
    # tab: today's behaviour — every row, tags, no summary.
    close_settings_sheets(authed_page)
    authed_page.locator("#tabClaude").click()
    expect(authed_page.locator("#sessionsList li.session-item")).to_have_count(3)
    expect(authed_page.locator("#sessionsList .session-channel-tag")).to_have_count(2)
    expect(authed_page.locator("#sessionsChannelSummary")).to_be_hidden()
    row = authed_page.locator('#sessionsList li[data-session-id="s-tg-health"]')
    expect(row.locator(".action-stop-close")).to_have_count(1)

    authed_page.locator("#tabBoard").click()
    expect(authed_page.locator("#boardColumns li.board-item")).to_have_count(3)


def test_the_one_kill_path_refuses_a_hidden_channel_session(
    authed_page: Page, base_url: str
) -> None:
    """Whatever UI reaches ``stopSession`` -- the Board drawer passes a stripped
    ``{session_id, name}`` -- a hidden Telegram session is not stopped."""
    _mock(authed_page)
    stops: list = []
    authed_page.route(
        re.compile(r".*/api/claude-code/sessions/[^/]+/stop$"),
        lambda route: (stops.append(route.request.url),
                       route.fulfill(status=200, content_type="application/json", body="{}")),
    )
    _open_coding(authed_page, base_url)
    expect(authed_page.locator("#sessionsChannelSummary")).to_be_visible()

    authed_page.evaluate(
        """async () => {
          const { stopSession } = await import('/static/sessions.js');
          await stopSession({ session_id: 's-tg-health', name: 'life-os' });
        }"""
    )
    flush_requests(authed_page)
    assert stops == [], f"a hidden Telegram session was stopped: {stops}"
    expect(authed_page.locator("#toast")).to_contain_text("Telegram sessions are protected")


# ------------------------------------------------------------------ #1402


def _context_chip(page: Page):
    """The Code tab's row says it with an attention chip since #1434."""
    return page.locator("#sessionsChannelSummary .channel-context-chip")


def test_summary_does_not_alert_one_point_under_fifty_percent(
    authed_page: Page, base_url: str
) -> None:
    knobs = _mock(authed_page)
    knobs["pct"] = {"s-tg-health": 49, "s-tg-family": 20}
    _open_coding(authed_page, base_url)
    expect(authed_page.locator("#sessionsChannelSummary")).to_be_visible()
    wait_until(authed_page, lambda: len(knobs["context_hits"]) >= 2,
               "the summary line's context reads")
    flush_requests(authed_page)
    expect(_context_chip(authed_page)).to_have_count(0)


def test_summary_shows_the_context_chip_at_fifty_percent(
    authed_page: Page, base_url: str
) -> None:
    knobs = _mock(authed_page)
    knobs["pct"] = {"s-tg-health": 42, "s-tg-family": 50}
    _open_coding(authed_page, base_url)
    # Code: an attention chip naming the highest figure (#1434).
    chip = _context_chip(authed_page)
    expect(chip).to_have_text("context 50%")
    expect(chip).to_have_attribute("data-tone", "attention")


def test_alert_follows_the_context_as_it_changes(
    authed_page: Page, base_url: str
) -> None:
    _fake_clock.install(authed_page)
    knobs = _mock(authed_page)
    knobs["pct"] = {"s-tg-health": 55}
    _open_coding(authed_page, base_url)
    _fake_clock.wait_for_interval(authed_page, 5000)
    expect(_context_chip(authed_page)).to_have_text("context 55%")

    # The session compacts: its next slow read is under the threshold and the
    # icon goes away without a reload.
    knobs["pct"] = {"s-tg-health": 8}
    _fake_clock.advance(authed_page, 60_000)
    flush_requests(authed_page)
    expect(_context_chip(authed_page)).to_have_count(0)


def test_popup_compact_sends_slash_compact_through_the_verified_route(
    authed_page: Page, base_url: str
) -> None:
    knobs = _mock(authed_page)
    knobs["pct"] = {"s-tg-health": 71, "s-tg-family": 12}
    _open_coding(authed_page, base_url)
    authed_page.locator("#sessionsChannelSummary").click()
    dialog = authed_page.locator("#channelListDialog")
    expect(dialog).to_be_visible()

    high = dialog.locator('li[data-session-id="s-tg-health"] .channel-list-compact')
    low = dialog.locator('li[data-session-id="s-tg-family"] .channel-list-compact')
    # On every running row, as a borderless icon-only control with no text
    # label; the one at >= 50 % is highlighted by colour on the icon itself.
    expect(high).to_have_count(1)
    expect(low).to_have_count(1)
    expect(high).to_have_text("")
    expect(high.locator("svg")).to_have_count(1)
    name = dialog.locator(
        'li[data-session-id="s-tg-health"] .channel-list-name').inner_text()
    expect(high).to_have_attribute("aria-label", f"Compact {name}")
    expect(high).to_have_attribute("data-high", "true")
    expect(low).not_to_have_attribute("data-high", "true")
    expect(high).to_have_css("border-top-width", "0px")
    expect(high).to_have_css("background-color", "rgba(0, 0, 0, 0)")
    assert stable_eval(high, "e => getComputedStyle(e).color") != stable_eval(
        low, "e => getComputedStyle(e).color"), "the >= 50 % highlight is not on the icon"
    # A 44x44 hit area at the right end of the row itself, not under it.
    geo = stable_eval(dialog.locator('li[data-session-id="s-tg-health"]'), """li => {
      const r = li.getBoundingClientRect();
      const o = li.querySelector('.channel-list-open').getBoundingClientRect();
      const c = li.querySelector('.channel-list-compact').getBoundingClientRect();
      return { row: [r.left, r.top, r.right, r.bottom],
               look: [o.left, o.top, o.right, o.bottom],
               btn: [c.left, c.top, c.right, c.bottom] };
    }""")
    row, look, btn = geo["row"], geo["look"], geo["btn"]
    assert btn[2] - btn[0] >= 44 and btn[3] - btn[1] >= 44, f"not a 44px target: {btn}"
    assert btn[0] >= look[2] - 1, f"Compact is not after the row's content: {btn} {look}"
    assert btn[2] <= row[2] + 1, f"Compact overflows the row: {btn} {row}"
    assert btn[1] >= row[1] - 1 and btn[3] <= row[3] + 1, \
        f"Compact sits outside the row's height: {btn} {row}"

    high.click()
    wait_until(authed_page, lambda: len(knobs["inputs"]) == 1, "the /input POST")
    assert knobs["inputs"] == [("s-tg-health", {"data": "/compact", "submit": True})]
    expect(authed_page.locator("#toast")).to_contain_text("Compact: Sent")
    # The outcome rides on the row (a modal <dialog> paints over the toast)
    # without making it taller: it takes the place of the status word.
    status = dialog.locator('li[data-session-id="s-tg-health"] .channel-list-status')
    expect(status).to_have_text("Sent")
    after = stable_eval(dialog.locator('li[data-session-id="s-tg-health"]'),
                        "li => li.getBoundingClientRect().height")
    assert abs(after - (row[3] - row[1])) < 1, f"the row grew: {row[3] - row[1]} -> {after}"
    # Compact acts; it does not open the read-only look underneath.
    expect(authed_page.locator("#terminalOverlay")).to_be_hidden()
    expect(dialog).to_be_visible()


def test_popup_has_compact_but_still_no_stop_or_delete(
    authed_page: Page, base_url: str
) -> None:
    _mock(authed_page)
    _open_coding(authed_page, base_url)
    authed_page.locator("#sessionsChannelSummary").click()
    dialog = authed_page.locator("#channelListDialog")
    expect(dialog.locator("li.channel-list-row")).to_have_count(2)
    expect(dialog.locator(".channel-list-compact")).to_have_count(2)
    labels = dialog.locator("button").all_inner_texts()
    assert not [t for t in labels if re.search(r"stop|delete|kill|remove", t, re.I)], labels
    expect(dialog.locator(".action-stop-close, .board-stop-btn")).to_have_count(0)
