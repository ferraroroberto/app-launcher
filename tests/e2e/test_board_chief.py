"""Fleet chief e2e (issue #245).

Browser-side coverage of the Board's chat mode and chief card: the chat
segment reroutes the dispatch bar's send to ensure-then-reply (the message
rides the same input proxy as drawer replies, never /api/board/dispatch),
a mocked chief reply renders through the drawer's exchange surface, the
chief card is visually distinct and confirm-protected against the one-tap
stop every other card keeps, the manual Start affordance shows when no
chief is alive (Restart when one is, #617), and the settings dialog
round-trips GET → edit → PUT.
Hermetic — board/exchange/ensure/settings are route-mocked before goto,
per the #510 convention (mock non-deterministic boot fetches first).

Server-side logic (spawn shape, label matching, fresh respawn, settings
validation) is covered by tests/test_chief_ensure.py.
"""

from __future__ import annotations

import copy
import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke


_CHIEF_CARD = {
    "session_id": "s-chief", "kind": "pty", "agent": "claude",
    "label": "chief", "project_dir": "E:/automation/fleet-config",
    "name": "chief", "alive": True, "started_at": "2026-07-18T06:00:00Z",
    "live_title": "", "prompt_title": "", "manual_title": "chief",
    "project": "fleet-config", "status": "idle", "age_seconds": 60,
}

_WORKER_CARD = {
    "session_id": "s-work", "kind": "pty", "agent": "claude",
    "project_dir": "E:/automation/life-os", "name": "life-os",
    "alive": True, "started_at": "2026-07-18T06:30:00Z",
    "live_title": "weekly recap", "prompt_title": "",
    "project": "life-os", "status": "working", "age_seconds": 240,
}

_BOARD_BASE = {
    "generated_at": "2026-07-18T07:00:00Z",
    "columns": {
        "backlog": [], "claude_turn": [], "your_turn": [], "other": [],
        "done": [],
    },
    "github": {"fetched_at": "2026-07-18T06:59:00Z", "error": None},
    "sessions_state": {"available": True, "stale": False,
                       "updated_at": "2026-07-18T06:59:30Z"},
}

_CHIEF_EXCHANGE = {
    "available": True,
    "source": "native",
    "reason": None,
    "user": {"text": "what's open in app-launcher?",
             "timestamp": "2026-07-18T07:01:00Z"},
    "assistant": {"text": "6 open issues. Smallest is #229 — start it?",
                  "timestamp": "2026-07-18T07:01:30Z"},
}


def _board_payload(*, with_chief: bool) -> dict:
    payload = copy.deepcopy(_BOARD_BASE)
    if with_chief:
        payload["columns"]["claude_turn"] = [copy.deepcopy(_CHIEF_CARD)]
    payload["columns"]["claude_turn"].append(copy.deepcopy(_WORKER_CARD))
    # Stamp gh fresh at real-clock time so tab open never auto-refreshes.
    from datetime import datetime, timezone
    payload["github"]["fetched_at"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    return payload


def _mock_board(page: Page, payload: dict) -> None:
    body = _json.dumps(payload)
    page.route(
        re.compile(r".*/api/board$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=body,
        ),
    )
    page.route(
        re.compile(r".*/api/board/github/refresh$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"fetched_at": payload["github"]["fetched_at"],
                              "error": None}),
        ),
    )
    # Boot-time git-status is git-subprocess-backed and lands whenever it
    # likes; a real response mid-interaction rebuilds the drawer DOM out
    # from under a click (#510/#512) — mock it deterministic.
    page.route(
        re.compile(r".*/api/claude-code/git-status$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"projects": []}),
        ),
    )


def _mock_exchange(page: Page, sid: str = "s-chief") -> None:
    page.route(
        re.compile(r".*/api/board/sessions/" + sid + r"/exchange.*"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(_CHIEF_EXCHANGE),
        ),
    )


def _mock_ensure(
    page: Page, captured: dict, *, spawned: bool = False,
    resumed: bool = False, resume_fallback_reason: str = "",
) -> None:
    def _capture(route):
        captured["method"] = route.request.method
        captured["body"] = route.request.post_data_json
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "session_id": "s-chief", "spawned": spawned,
                "resumed": resumed,
                "resume_fallback_reason": resume_fallback_reason,
            }),
        )
    page.route(re.compile(r".*/api/board/chief/ensure$"), _capture)


_CHIEF_SETTINGS = {
    "settings": {"model": "fable", "worker_cap": 3},
}


def _open_board(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#tabBoard", state="attached", timeout=5_000)
    page.locator("#tabBoard").click()
    expect(page.locator("#paneBoard")).to_be_visible()


def _enter_chat_mode(page: Page) -> None:
    # Mode collapsed from a 4-segment radiogroup into a <select> in #547 —
    # the segments no longer fit an iPhone-width row.
    page.locator("#boardDispatchMode").select_option("chat")


def test_chat_mode_routes_message_to_chief_not_dispatch(
    authed_page: Page, base_url: str
) -> None:
    """Chat mode send = ensure → input proxy ({data, submit:true}); the
    one-shot /api/board/dispatch is never touched; the box clears
    (conversation semantics, unlike dispatch's keep-for-multi-dispatch)."""
    _mock_board(authed_page, _board_payload(with_chief=True))
    _mock_exchange(authed_page)
    ensured: dict = {}
    _mock_ensure(authed_page, ensured)

    captured_input: dict = {}

    def _capture_input(route):
        captured_input["method"] = route.request.method
        captured_input["body"] = route.request.post_data_json
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"ok": True, "bytes": 8, "submit": True}),
        )

    authed_page.route(
        re.compile(r".*/api/claude-code/sessions/s-chief/input$"),
        _capture_input,
    )

    dispatch_hits: list[str] = []
    authed_page.route(
        re.compile(r".*/api/board/dispatch$"),
        lambda route: (dispatch_hits.append(route.request.method),
                       route.fulfill(status=500, body="must not be called")),
    )

    _open_board(authed_page, base_url)
    _enter_chat_mode(authed_page)

    # Chat mode: the model select greys out (chief model is owned by chief
    # settings) and the status row appears.
    expect(authed_page.locator("#boardDispatchModel")).to_be_disabled()
    expect(authed_page.locator("#boardChiefStatus")).to_be_visible()

    authed_page.locator("#boardDispatchGoal").fill("what's open in app-launcher?")
    authed_page.locator("#boardDispatchSend").click()
    authed_page.wait_for_timeout(600)

    assert ensured.get("method") == "POST", "send never ensured the chief"
    assert captured_input.get("body") == {
        "data": "what's open in app-launcher?", "submit": True,
    }
    assert dispatch_hits == [], "chat mode must never hit /api/board/dispatch"
    expect(authed_page.locator("#boardDispatchGoal")).to_have_value("")

    # The chief's drawer opened so the reply has somewhere to land.
    expect(authed_page.locator(".board-drawer")).to_be_visible()


def test_chief_card_distinct_and_mocked_reply_renders_in_drawer(
    authed_page: Page, base_url: str
) -> None:
    _mock_board(authed_page, _board_payload(with_chief=True))
    _mock_exchange(authed_page)

    _open_board(authed_page, base_url)
    chief_li = authed_page.locator("li.board-item-chief")
    expect(chief_li).to_be_visible()
    expect(chief_li).to_contain_text("chief")
    # Crown glyph marks the card (accent tint is the .board-item-chief class).
    assert chief_li.locator(
        '.board-chief-crown use[href="#i-crown"]'
    ).count() == 1

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
    keeping the crown and the chat status row truthful."""
    payload = _board_payload(with_chief=True)
    del payload["columns"]["claude_turn"][0]["label"]
    _mock_board(authed_page, payload)

    _open_board(authed_page, base_url)
    expect(authed_page.locator("li.board-item-chief")).to_be_visible()
    _enter_chat_mode(authed_page)
    expect(authed_page.locator("#boardChiefStatus")).not_to_contain_text(
        "not running"
    )
    expect(authed_page.locator("#boardChiefStart")).to_be_hidden()


def test_chief_stop_requires_confirm_other_cards_do_not(
    authed_page: Page, base_url: str
) -> None:
    """#245 kill protection: the chief's ✕ asks first (dismiss → no stop,
    accept → stop); a worker card keeps the deliberate one-tap stop (#253)
    with no dialog at all."""
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

    # 1. Chief + dismiss → drawer stays, stop never fires.
    authed_page.once("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    authed_page.locator("li.board-item-chief button.board-card").click()
    authed_page.locator(".board-stop-btn").click()
    authed_page.wait_for_timeout(400)
    assert len(dialogs) == 1 and "chief" in dialogs[0].lower()
    assert stops == [], "dismissing the confirm must not stop the chief"
    expect(authed_page.locator(".board-drawer")).to_be_visible()

    # 2. Chief + accept → stop fires.
    authed_page.once("dialog", lambda d: (dialogs.append(d.message), d.accept()))
    authed_page.locator(".board-stop-btn").click()
    authed_page.wait_for_timeout(600)
    assert len(dialogs) == 2
    assert len(stops) == 1 and "/sessions/s-chief/stop" in stops[0]["url"]

    # 3. Worker card → one-tap stop, no dialog. (An unexpected confirm would
    # be auto-dismissed by Playwright and show up as a missing stop call.)
    authed_page.locator(
        "li.board-item:not(.board-item-chief) button.board-card"
    ).first.click()
    authed_page.locator(".board-stop-btn").click()
    authed_page.wait_for_timeout(600)
    assert len(dialogs) == 2, "a worker stop must not raise a confirm"
    assert len(stops) == 2 and "/sessions/s-work/stop" in stops[1]["url"]


def test_chat_mode_send_reattaches_resumable_chief(
    authed_page: Page, base_url: str
) -> None:
    """#651: the lazy first-send ensure used to spawn a blank chief with no
    resume flag, silently discarding a resumable conversation exactly like
    Restart did before #649/#650 — and the lazy send is in fact the most
    likely path a user takes after a session-host restart, since typing
    into chat mode reads as conversational and the Start/Resume status row
    is easy to miss. The send must POST ensure with resume:true, and toast
    'Chief resumed' when the response comes back resumed."""
    _mock_board(authed_page, _board_payload(with_chief=False))
    ensured: dict = {}
    _mock_ensure(authed_page, ensured, spawned=True, resumed=True)

    authed_page.route(
        re.compile(r".*/api/claude-code/sessions/s-chief/input$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"ok": True, "bytes": 8, "submit": True}),
        ),
    )

    _open_board(authed_page, base_url)
    _enter_chat_mode(authed_page)

    authed_page.locator("#boardDispatchGoal").fill("hey")
    authed_page.locator("#boardDispatchSend").click()
    authed_page.wait_for_timeout(600)

    assert ensured.get("method") == "POST", "send never POSTed ensure"
    assert ensured.get("body", {}).get("resume") is True
    assert ensured["body"].get("fresh") is not True, (
        "chat send must never force-kill a live chief"
    )
    expect(authed_page.locator("#toast")).to_contain_text("Chief resumed")


def test_chat_mode_send_toasts_fresh_spawn_when_nothing_resumable(
    authed_page: Page, base_url: str
) -> None:
    """#651: when nothing is resumable the send still degrades to a fresh
    spawn, but the toast must say so — the 'Chief spawned' wording used to
    fire unconditionally regardless of whether a resume actually happened."""
    _mock_board(authed_page, _board_payload(with_chief=False))
    ensured: dict = {}
    _mock_ensure(authed_page, ensured, spawned=True, resumed=False)

    authed_page.route(
        re.compile(r".*/api/claude-code/sessions/s-chief/input$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"ok": True, "bytes": 8, "submit": True}),
        ),
    )

    _open_board(authed_page, base_url)
    _enter_chat_mode(authed_page)

    authed_page.locator("#boardDispatchGoal").fill("hey")
    authed_page.locator("#boardDispatchSend").click()
    authed_page.wait_for_timeout(600)

    assert ensured.get("body", {}).get("resume") is True
    expect(authed_page.locator("#toast")).to_contain_text("Chief spawned")


def test_chat_mode_offers_manual_start_when_chief_down(
    authed_page: Page, base_url: str
) -> None:
    _mock_board(authed_page, _board_payload(with_chief=False))
    ensured: dict = {}
    _mock_ensure(authed_page, ensured, spawned=True)

    _open_board(authed_page, base_url)
    _enter_chat_mode(authed_page)

    row = authed_page.locator("#boardChiefStatus")
    expect(row).to_be_visible()
    expect(row).to_contain_text("not running")
    start = authed_page.locator("#boardChiefStart")
    expect(start).to_be_visible()
    start.click()
    authed_page.wait_for_timeout(500)
    assert ensured.get("method") == "POST", "Start never POSTed ensure"


def test_chat_mode_offers_restart_when_chief_alive(
    authed_page: Page, base_url: str
) -> None:
    """#617: Start and Restart are mutually exclusive on actual state — a
    live chief shows Restart (never Start, which would offer to spawn a
    duplicate). #649: clicking it confirms, then POSTs ensure with
    fresh:true AND resume:true — the graceful stop-then-resume-the-same-
    conversation (never the session-host restart, and never a silent
    discard of the conversation in favor of a blank fresh one)."""
    _mock_board(authed_page, _board_payload(with_chief=True))
    _mock_exchange(authed_page)
    ensured: dict = {}
    _mock_ensure(authed_page, ensured, spawned=True, resumed=True)

    _open_board(authed_page, base_url)
    _enter_chat_mode(authed_page)

    expect(authed_page.locator("#boardChiefStart")).to_be_hidden()
    restart = authed_page.locator("#boardChiefRestart")
    expect(restart).to_be_visible()

    authed_page.once("dialog", lambda d: d.accept())
    restart.click()
    authed_page.wait_for_timeout(500)

    assert ensured.get("method") == "POST", "Restart never POSTed ensure"
    assert ensured.get("body", {}).get("fresh") is True
    assert ensured.get("body", {}).get("resume") is True
    expect(authed_page.locator("#toast")).to_contain_text("Chief resumed")


def test_chat_mode_restart_toasts_fallback_when_nothing_resumable(
    authed_page: Page, base_url: str
) -> None:
    """#649: when the ensure response comes back with resumed:false (no
    resumable conversation within the 24h window), Restart still degrades
    to a fresh spawn rather than failing — but the toast must say so
    explicitly, reusing the Resume button's existing fallback wording, so
    the user is never left assuming a resume happened when it didn't."""
    _mock_board(authed_page, _board_payload(with_chief=True))
    _mock_exchange(authed_page)
    ensured: dict = {}
    _mock_ensure(
        authed_page, ensured, spawned=True, resumed=False,
        resume_fallback_reason="no resumable chief conversation found in the last 24h",
    )

    _open_board(authed_page, base_url)
    _enter_chat_mode(authed_page)

    authed_page.once("dialog", lambda d: d.accept())
    authed_page.locator("#boardChiefRestart").click()
    authed_page.wait_for_timeout(500)

    assert ensured.get("body", {}).get("resume") is True
    expect(authed_page.locator("#toast")).to_contain_text(
        "No resumable conversation"
    )


def test_chief_settings_dialog_roundtrip(
    authed_page: Page, base_url: str
) -> None:
    """Gear → GET-populated fields; edit worker cap → Save PUTs the settings
    body; × path (Cancel) just closes. #616 retired the daily-respawn
    fields — model and worker cap are all that's left to round-trip."""
    _mock_board(authed_page, _board_payload(with_chief=True))

    put: dict = {}

    def _settings(route):
        if route.request.method == "PUT":
            put["body"] = route.request.post_data_json
            route.fulfill(
                status=200, content_type="application/json",
                body=_json.dumps({"settings": put["body"]}),
            )
        else:
            route.fulfill(
                status=200, content_type="application/json",
                body=_json.dumps(_CHIEF_SETTINGS),
            )

    authed_page.route(re.compile(r".*/api/board/chief/settings$"), _settings)

    _open_board(authed_page, base_url)
    _enter_chat_mode(authed_page)
    authed_page.locator("#boardChiefSettings").click()

    dialog = authed_page.locator("#chiefSettingsDialog")
    expect(dialog).to_be_visible()
    expect(authed_page.locator("#chiefModelSelect")).to_have_value("fable")
    expect(authed_page.locator("#chiefWorkerCap")).to_have_value("3")

    authed_page.locator("#chiefWorkerCap").fill("5")
    authed_page.locator('#chiefSettingsForm button[type="submit"]').click()
    authed_page.wait_for_timeout(500)

    assert put.get("body") == {"model": "fable", "worker_cap": 5}
    expect(dialog).not_to_be_visible()
