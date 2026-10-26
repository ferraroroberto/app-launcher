"""The chief's Board card: its wording, the drawer reply, Stop, the settings sheet and a drawer that survives polling."""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import _fake_clock
from tests.e2e.fixtures.ui import open_row_menu, open_settings_sheet
from tests.e2e.fixtures.waits import flush_requests, stable_eval, wait_until

from tests.e2e.board._chief_support import (
    _board_payload,
    _mock_board,
    _mock_board_side_routes,
    _mock_exchange,
    _open_board,
)


pytestmark = pytest.mark.smoke


@pytest.mark.iphone
def test_chief_card_distinct_and_mocked_reply_renders_in_drawer(
    authed_page: Page, base_url: str
) -> None:
    _mock_board(authed_page, _board_payload(with_chief=True))
    _mock_exchange(authed_page)

    _open_board(authed_page, base_url)
    chief_li = authed_page.locator("li.board-item-chief")
    expect(chief_li).to_be_visible()
    expect(chief_li).to_contain_text("chief")
    # With the chief running its plan reads as current (#1279): the rows,
    # and no "not running" line. An absent updated_at is said, not hidden.
    plan_body = authed_page.locator("#boardChiefPlan .board-plan-body")
    row = plan_body.locator("li.board-plan-row")
    expect(row.locator(".action-row-title")).to_have_text("chief's plan card")
    # No ref_url in this plan: the ref stays plain text and the row inert
    # (no tap, no chevron), never a dead link.
    expect(row.locator(".action-row-meta-text")).to_have_text("app-launcher#1279")
    expect(row.locator("a")).to_have_count(0)
    expect(row.locator(".action-row-main")).to_have_class(re.compile(r"\bis-inert\b"))
    expect(row.locator(".board-plan-chevron")).to_have_count(0)
    expect(plan_body.locator(".board-plan-age")).to_have_text("Update time unknown")
    expect(plan_body.locator(".board-plan-chief")).to_have_count(0)
    # The crown glyph marks the card; it carries no tint of its own (#1449):
    # its background and meta colour equal a worker session card's.
    assert chief_li.locator(
        '.board-chief-crown use[href="#i-crown"]'
    ).count() == 1
    # Scoped to Claude's turn: the chief's plan, whose rows are board items
    # too, sits above it since #1436.
    worker_li = authed_page.locator(
        '.board-list[data-col="claude_turn"] li.board-item:not(.board-item-chief)').first
    expect(worker_li).to_be_visible()
    read = ("el => { const c = el.querySelector('button.board-card');"
            " const m = el.querySelector('.board-card-meta');"
            " return [getComputedStyle(c).backgroundColor,"
            " getComputedStyle(m).color]; }")
    chief_bg, chief_meta = stable_eval(chief_li, read)
    worker_bg, worker_meta = stable_eval(worker_li, read)
    assert chief_bg == worker_bg
    assert chief_meta == worker_meta

    chief_li.locator("button.board-card").click()
    drawer = authed_page.locator(".board-drawer")
    expect(drawer).to_be_visible()
    expect(drawer.locator(".board-exchange")).to_have_attribute(
        "data-state", "ready"
    )
    expect(drawer).to_contain_text("6 open issues. Smallest is #229 — start it?")


def test_chief_needs_you_card_reads_standing_by_not_needs_you(
    authed_page: Page, base_url: str
) -> None:
    """#575: a needs-you-family chief card (its normal resting state between
    dispatches) must not read as an alert. Server already routes it into
    Claude's turn regardless of status; the client relabels the text. #608
    split needs-you into four values — idle-finished is the one that most
    directly matches "chief just finished replying, standing by"."""
    payload = _board_payload(with_chief=True)
    payload["columns"]["claude_turn"][0]["status"] = "idle-finished"
    _mock_board(authed_page, payload)

    _open_board(authed_page, base_url)
    chief_li = authed_page.locator("li.board-item-chief")
    expect(chief_li).to_be_visible()
    expect(chief_li).to_contain_text("standing by")
    expect(chief_li).not_to_contain_text("needs you")
    expect(authed_page.locator("#boardColYours .board-count")).to_have_text("0")


def test_chief_recognized_by_name_when_label_missing(
    authed_page: Page, base_url: str
) -> None:
    """Legacy-host fallback: a session-host that predates the label field
    reports no ``label`` on the chief's card — the client must still
    recognize it by launch name (mirror of the server's _find_chief),
    keeping the crown truthful."""
    payload = _board_payload(with_chief=True)
    del payload["columns"]["claude_turn"][0]["label"]
    _mock_board(authed_page, payload)

    _open_board(authed_page, base_url)
    chief = authed_page.locator("li.board-item-chief")
    expect(chief).to_be_visible()
    expect(chief.locator(".board-chief-crown")).to_have_count(1)


def test_stop_always_confirms_and_the_chief_keeps_its_wording(
    authed_page: Page, base_url: str
) -> None:
    """#245 kill protection, widened by #1502: Stop, in the drawer's kebab,
    asks first for every session (dismiss → no stop, accept → stop). The
    chief keeps its own "Kill the chief session?" words; a worker gets a
    plain "Stop <name>?"."""
    _mock_board(authed_page, _board_payload(with_chief=True))
    _mock_exchange(authed_page, sid="s-chief")
    _mock_exchange(authed_page, sid="s-work")

    stops: list[dict] = []

    def _capture_stop(route):
        stops.append({"url": route.request.url,
                      "body": route.request.post_data_json})
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"ok": True}),
        )

    authed_page.route(
        re.compile(r".*/api/claude-code/sessions/[^/]+/stop$"), _capture_stop
    )

    dialogs: list[str] = []
    _open_board(authed_page, base_url)

    # The ask is the themed confirm (#1501), never a native one.
    authed_page.on("dialog", lambda d: dialogs.append(d.message))
    confirm = authed_page.locator("#confirmDialog")

    def _menu_stop():
        authed_page.locator(".board-drawer .board-drawer-kebab").click()
        open_row_menu(authed_page).locator("[data-item=board-stop-btn]").click()

    # 1. Chief + ✕ → drawer stays, stop never fires.
    authed_page.locator("li.board-item-chief button.board-card").click()
    _menu_stop()
    expect(confirm).to_be_visible()
    expect(authed_page.locator("#confirmDialogTitle")).to_have_text("Kill the chief session?")
    expect(authed_page.locator("#confirmDialogOk")).to_have_text("Kill")
    authed_page.locator("#confirmDialogClose").click()
    expect(confirm).to_be_hidden()
    flush_requests(authed_page)
    assert stops == [], "dismissing the confirm must not stop the chief"
    expect(authed_page.locator(".board-drawer")).to_be_visible()

    # 2. Chief + the action → stop fires.
    _menu_stop()
    expect(confirm).to_be_visible()
    authed_page.locator("#confirmDialogOk").click()
    wait_until(authed_page, lambda: len(stops) >= 1, "the confirmed chief stop POST")
    assert len(stops) == 1 and "/sessions/s-chief/stop" in stops[0]["url"]

    # 3. Worker card → asks too, in plain words; a dismiss stops nothing.
    authed_page.locator(
        "li.board-item:not(.board-item-chief) button.board-card"
    ).first.click()
    _menu_stop()
    expect(confirm).to_be_visible()
    expect(authed_page.locator("#confirmDialogTitle")).to_have_text(re.compile(r"^Stop .+\?$"))
    expect(authed_page.locator("#confirmDialogOk")).to_have_text("Stop")
    authed_page.locator("#confirmDialogClose").click()
    flush_requests(authed_page)
    assert len(stops) == 1, "dismissing the confirm must not stop the worker"
    _menu_stop()
    authed_page.locator("#confirmDialogOk").click()
    wait_until(authed_page, lambda: len(stops) >= 2, "the confirmed worker stop POST")
    assert len(stops) == 2 and "/sessions/s-work/stop" in stops[1]["url"]
    assert dialogs == [], f"a native dialog was raised: {dialogs}"


def test_chief_settings_sheet_roundtrip(
    authed_page: Page, base_url: str
) -> None:
    """Gear -> Settings > Chief sheet filled from GET /api/config; picking a
    model PUTs ``{"model"}``; committing the worker cap (blur) PUTs
    ``{"worker_cap"}``; an out-of-range cap sends nothing and says why.

    The Board's chief settings dialog became Settings > Chief in #1435: no
    Save button, each field saves on its own (#616 retired the daily-respawn
    fields — model and worker cap are all that is left to round-trip). The
    sheet reads the chief fields off GET /api/config now, so that read is
    patched here (every other /api/config call passes through to the
    disposable server) to keep the starting values deterministic."""
    _mock_board(authed_page, _board_payload(with_chief=True))

    stored = {"model": "fable", "worker_cap": 3}
    puts: list[dict] = []

    def _config(route):
        if route.request.method != "GET":
            route.continue_()
            return
        resp = route.fetch()
        body = resp.json()
        body.update(
            chief_model=stored["model"], chief_worker_cap=stored["worker_cap"],
            chief_worker_cap_min=1, chief_worker_cap_max=10,
        )
        route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(body),
        )

    def _settings(route):
        if route.request.method == "PUT":
            patch = route.request.post_data_json
            puts.append(patch)
            stored.update(patch)
            route.fulfill(
                status=200, content_type="application/json",
                body=_json.dumps({"settings": dict(stored)}),
            )
        else:
            route.fulfill(
                status=200, content_type="application/json",
                body=_json.dumps({"settings": dict(stored)}),
            )

    authed_page.route(re.compile(r".*/api/config$"), _config)
    authed_page.route(re.compile(r".*/api/board/chief/settings$"), _settings)

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    # The Board has no chief card (#1436): Settings › Chief is reached from
    # Settings itself, or the Code tab's chief row (test_coding_chief.py).
    open_settings_sheet(authed_page, "chiefSheet")

    expect(authed_page.locator("#paneSettings")).to_be_visible()
    expect(authed_page.locator("#chiefSheet")).to_be_visible()
    expect(authed_page.locator("#chiefSettingsDialog")).to_have_count(0)
    expect(authed_page.locator("#chiefModelSelect")).to_have_attribute(
        "data-value", "fable"
    )
    expect(authed_page.locator("#chiefWorkerCap")).to_have_value("3")

    # Model: one PUT carrying only the model.
    authed_page.locator("#chiefModelSelect .model-combo-trigger").click()
    authed_page.locator("#chiefModelMenu [data-value='opus']").click()
    wait_until(authed_page, lambda: puts == [{"model": "opus"}],
               "the chief model PUT")
    expect(authed_page.locator("#toast")).to_contain_text("Chief model: Opus.")
    expect(authed_page.locator("#chiefModelSelect")).to_have_attribute(
        "data-value", "opus"
    )

    # Worker cap: saves when the field is committed, carrying only the cap.
    cap = authed_page.locator("#chiefWorkerCap")
    cap.fill("5")
    cap.blur()
    wait_until(authed_page, lambda: puts == [{"model": "opus"}, {"worker_cap": 5}],
               "the worker cap PUT")
    expect(authed_page.locator("#toast")).to_contain_text("Worker cap saved.")

    # Out of range: nothing is sent, the toast and the field say why.
    cap.fill("11")
    cap.blur()
    expect(authed_page.locator("#toast")).to_contain_text(
        "Worker cap must be a whole number between 1 and 10."
    )
    expect(cap).to_have_attribute("aria-invalid", "true")
    flush_requests(authed_page)
    assert puts == [{"model": "opus"}, {"worker_cap": 5}], puts


def _capture_chief_input_post(page: Page) -> dict:
    """Route the chief's input POST, recording its method and body in the returned dict."""
    captured_input: dict = {}

    def _capture_input(route):
        captured_input["method"] = route.request.method
        captured_input["body"] = route.request.post_data_json
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"ok": True, "bytes": 8, "submit": True}),
        )

    page.route(
        re.compile(r".*/api/claude-code/sessions/s-chief/input$"),
        _capture_input,
    )
    return captured_input


def _payload_with_fresh_lane_and_closed_issue() -> dict:
    """The chief board payload plus a freshly dispatched lane and a closed issue."""
    changed = _board_payload(with_chief=True)
    changed["columns"]["claude_turn"].insert(0, {
        "session_id": "s-fresh", "kind": "pty", "agent": "claude",
        "project_dir": "E:/automation/whatsapp-radar", "name": "whatsapp-radar",
        "alive": True, "started_at": "2026-07-18T07:02:00Z",
        "live_title": "fresh lane", "prompt_title": "",
        "project": "whatsapp-radar", "status": "working", "age_seconds": 5,
    })
    changed["columns"]["done"] = [{
        "kind": "issue", "repo": "fleet-config", "number": 905,
        "title": "closed while chatting",
        "url": "https://github.com/ferraroroberto/fleet-config/issues/905",
        "updated_at": "2026-07-18T07:02:30Z", "state": "closed", "labels": [],
    }]
    return changed


def test_board_keeps_polling_with_chief_drawer_open_and_reply_survives(
    authed_page: Page, base_url: str
) -> None:
    """#958: chatting with the chief keeps its drawer open for the whole
    conversation, so a poll paused on any open drawer froze the card list
    for hours with no sign it was stale. The poll must keep landing — a
    lane dispatched and an issue closed show up within one poll interval —
    while the open drawer's own node (reply box text, focus) survives the
    re-render, which is the #301 typing guarantee the pause used to buy.

    The Board has no chief composer since #1436: the chief is reached through
    its own session row in Claude's turn, whose drawer carries the shared
    composer. A send there goes through the input proxy ({data, submit:true})
    like any session's. Its checks run right after the send, before the
    payload swap below."""
    board = {"body": _json.dumps(_board_payload(with_chief=True))}
    authed_page.route(
        re.compile(r".*/api/board(?:\?.*)?$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=board["body"],
        ),
    )
    _mock_board_side_routes(authed_page)
    _mock_exchange(authed_page)

    captured_input = _capture_chief_input_post(authed_page)

    _fake_clock.install(authed_page)
    _open_board(authed_page, base_url)

    # No chief card, composer or mode control on the Board (#1382, #1436);
    # the "Start with" combo stays live — it feeds the Backlog rows' one-tap
    # Start, not a dispatch.
    expect(authed_page.locator("#boardDispatchMode")).to_have_count(0)
    expect(authed_page.locator("#boardChiefCard, #boardDispatchGoal")).to_have_count(0)
    expect(authed_page.locator("#boardDispatchModel .model-combo-trigger")).to_be_enabled()

    # Open the chief's drawer from its row in Claude's turn and send.
    authed_page.locator("li.board-item-chief > button.board-card").click()
    drawer = authed_page.locator("li.board-item-chief .board-drawer")
    expect(drawer).to_be_visible()
    expect(drawer.locator(".board-exchange")).to_contain_text("6 open issues")
    reply = drawer.locator(".board-drawer-composer .composer-input")
    reply.fill("what's open in app-launcher?")
    drawer.locator(".board-drawer-composer .composer-send").click()
    wait_until(authed_page, lambda: captured_input.get("body") is not None,
               "the chief message's input POST")
    flush_requests(authed_page)
    assert captured_input.get("body") == {
        "data": "what's open in app-launcher?", "submit": True,
    }

    # -- #958: the poll keeps landing while the chief drawer stays open --
    # A send closes the drawer (sendFromDrawer, #461); reopen it to type on.
    expect(drawer).to_be_hidden()
    authed_page.locator("li.board-item-chief > button.board-card").click()
    expect(drawer).to_be_visible()
    reply.fill("half-typed follow-up")
    # Tag the live node: a drawer rebuilt by the poll loses the tag even
    # when it re-opens looking identical.
    drawer.evaluate("el => { el.dataset.e2eTag = 'pre-poll'; }")

    board["body"] = _json.dumps(_payload_with_fresh_lane_and_closed_issue())

    # One poll (BOARD_POLL_MS is 5 s), jumped on the fake clock (#1515).
    _fake_clock.jump_interval(authed_page, 5_000, "fetchBoard")
    expect(
        authed_page.locator('.board-list[data-col="claude_turn"]')
    ).to_contain_text("fresh lane")
    expect(
        authed_page.locator('.board-list[data-col="done"]')
    ).to_contain_text("closed while chatting")

    expect(drawer).to_have_attribute("data-e2e-tag", "pre-poll")
    expect(reply).to_have_value("half-typed follow-up")
    expect(reply).to_be_focused()
