"""Fleet chief e2e (issue #245).

Browser-side coverage of the Board's chat mode and chief card: the chat
segment reroutes the dispatch bar's send to ensure-then-reply (the message
rides the same input proxy as drawer replies, never /api/board/dispatch),
a mocked chief reply renders through the drawer's exchange surface, the
chief card is visually distinct and confirm-protected against the one-tap
stop every other card keeps, the manual Start affordance shows when no
chief is alive (Restart when one is, #617), and the settings dialog
round-trips GET → edit → PUT. The Chief's-plan card's answer sheet
(#1295) renders the chief's questions and sends every answer as one message
down that same input path.
Hermetic — board/exchange/ensure/settings are route-mocked before goto,
per the #510 convention (mock non-deterministic boot fetches first).

Server-side logic (spawn shape, label matching, fresh respawn, settings
validation) is covered by tests/test_chief_ensure.py.
"""

from __future__ import annotations

import base64
import copy
import json as _json
import re
from datetime import datetime, timezone

import pytest
from playwright.sync_api import Page, expect

from src import chief_plan
from tests.e2e.conftest import stable_read

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
    payload["github"]["fetched_at"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    return payload


def _mock_board(page: Page, payload: dict) -> None:
    body = _json.dumps(payload)
    page.route(
        re.compile(r".*/api/board(?:\?.*)?$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=body,
        ),
    )
    _mock_board_side_routes(page)


_PLAN = {
    "state": "ok", "updated_at": "", "lanes": [], "waiting_on_roberto": [],
    "queue": [{"repo": "app-launcher", "ref": "#1279", "title": "chief's plan card",
               "status": "building", "note": ""}],
}


def _mock_board_side_routes(page: Page) -> None:
    """Everything _mock_board stubs besides /api/board itself, for a test
    that serves a payload it changes mid-test."""
    # The chief's plan (#1279), which rides the Board poll.
    page.route(
        re.compile(r".*/api/board/chief-plan$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(_PLAN),
        ),
    )
    page.route(
        re.compile(r".*/api/board/github/refresh$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds").replace("+00:00", "Z"), "error": None}),
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
    # Mode collapsed from a 4-segment radiogroup into a <select> in #547 (the
    # segments no longer fit an iPhone-width row), then into the same
    # .model-combo as the model picker beside it in #869 — so it is driven
    # like that combo, not with select_option.
    page.locator("#boardDispatchMode .model-combo-trigger").click()
    page.locator("#boardDispatchModeMenu [data-value='chat']").click()
    expect(page.locator("#boardDispatchMode")).to_have_attribute("data-value", "chat")


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
    expect(row.locator(".board-card-title-compact")).to_have_text("chief's plan card")
    # No ref_url in this plan: the ref stays plain text, never a dead link.
    expect(row.locator(".board-card-meta-inline")).to_have_text("app-launcher#1279")
    expect(row.locator("a")).to_have_count(0)
    expect(plan_body.locator(".board-plan-age")).to_have_text("Update time unknown")
    expect(plan_body.locator(".board-plan-chief")).to_have_count(0)
    # Crown glyph marks the card (accent tint is the .board-item-chief class).
    assert chief_li.locator(
        '.board-chief-crown use[href="#i-crown"]'
    ).count() == 1
    # The tint's meta line takes --accent-text, the spec's text on
    # accent-soft: --muted read 4.17:1 there (#1175, COLOR-02).
    accent_text = authed_page.evaluate(
        "() => { const s = document.createElement('span');"
        " s.style.color = 'var(--accent-text)'; document.body.appendChild(s);"
        " const c = getComputedStyle(s).color; s.remove(); return c; }")
    expect(chief_li.locator(".board-card-meta").first).to_have_css("color", accent_text)

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


@pytest.mark.parametrize("resumed, toast", [
    pytest.param(True, "Chief resumed", id="resumable"),
    pytest.param(False, "Chief spawned", id="nothing-resumable"),
])
def test_chat_mode_send_ensures_with_resume_and_toasts_outcome(
    authed_page: Page, base_url: str, resumed: bool, toast: str
) -> None:
    """#651: the lazy first-send ensure used to spawn a blank chief with no
    resume flag, silently discarding a resumable conversation exactly like
    Restart did before #649/#650 — and the lazy send is in fact the most
    likely path a user takes after a session-host restart, since typing
    into chat mode reads as conversational and the Start/Resume status row
    is easy to miss. The send must POST ensure with resume:true, and toast
    'Chief resumed' when the response comes back resumed. When nothing is
    resumable the send still degrades to a fresh spawn, but the toast must
    say so — the 'Chief spawned' wording used to fire unconditionally
    regardless of whether a resume actually happened."""
    _mock_board(authed_page, _board_payload(with_chief=False))
    ensured: dict = {}
    _mock_ensure(authed_page, ensured, spawned=True, resumed=resumed)

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
    # #1351: a send never asks to stop a live chief.
    assert ensured["body"].get("restart") is not True
    expect(authed_page.locator("#toast")).to_contain_text(toast)


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


def test_resume_button_sends_explicit_restart_intent(
    authed_page: Page, base_url: str
) -> None:
    """#1351: ``resume`` alone now keeps a live chief server-side, so the
    Resume button carries ``restart`` to keep #633's stop-then-resume when a
    chief came up after the button was drawn."""
    _mock_board(authed_page, _board_payload(with_chief=False))
    ensured: dict = {}
    _mock_ensure(authed_page, ensured, spawned=True, resumed=True)

    _open_board(authed_page, base_url)
    _enter_chat_mode(authed_page)

    resume = authed_page.locator("#boardChiefResume")
    expect(resume).to_be_visible()
    resume.click()
    expect(authed_page.locator("#toast")).to_contain_text("Chief resumed")
    assert ensured.get("body", {}).get("resume") is True
    assert ensured["body"].get("restart") is True
    assert ensured["body"].get("fresh") is not True


@pytest.mark.parametrize("resumed, fallback_reason, toast", [
    pytest.param(True, "", "Chief resumed", id="resumable"),
    pytest.param(
        False, "no resumable chief conversation found in the last 24h",
        "No resumable conversation", id="nothing-resumable",
    ),
])
def test_chat_mode_offers_restart_when_chief_alive(
    authed_page: Page, base_url: str, resumed: bool, fallback_reason: str,
    toast: str,
) -> None:
    """#617: Start and Restart are mutually exclusive on actual state — a
    live chief shows Restart (never Start, which would offer to spawn a
    duplicate). #649: clicking it confirms, then POSTs ensure with
    fresh:true AND resume:true — the graceful stop-then-resume-the-same-
    conversation (never the session-host restart, and never a silent
    discard of the conversation in favor of a blank fresh one). When the
    ensure response comes back with resumed:false (no resumable conversation
    within the 24h window), Restart still degrades to a fresh spawn rather
    than failing — but the toast must say so explicitly, reusing the Resume
    button's existing fallback wording, so the user is never left assuming a
    resume happened when it didn't."""
    _mock_board(authed_page, _board_payload(with_chief=True))
    _mock_exchange(authed_page)
    ensured: dict = {}
    _mock_ensure(
        authed_page, ensured, spawned=True, resumed=resumed,
        resume_fallback_reason=fallback_reason,
    )

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
    expect(authed_page.locator("#toast")).to_contain_text(toast)


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
    expect(authed_page.locator("#chiefModelSelect")).to_have_attribute(
        "data-value", "fable"
    )
    expect(authed_page.locator("#chiefWorkerCap")).to_have_value("3")

    authed_page.locator("#chiefModelSelect .model-combo-trigger").click()
    authed_page.locator("#chiefModelMenu [data-value='opus']").click()
    authed_page.locator("#chiefWorkerCap").fill("5")
    authed_page.locator('#chiefSettingsForm button[type="submit"]').click()
    authed_page.wait_for_timeout(500)

    assert put.get("body") == {"model": "opus", "worker_cap": 5}
    expect(dialog).not_to_be_visible()


def test_board_keeps_polling_with_chief_drawer_open_and_reply_survives(
    authed_page: Page, base_url: str
) -> None:
    """#958: chatting with the chief keeps its drawer open for the whole
    conversation, so a poll paused on any open drawer froze the card list
    for hours with no sign it was stale. The poll must keep landing — a
    lane dispatched and an issue closed show up within one poll interval —
    while the open drawer's own node (reply box text, focus) survives the
    re-render, which is the #301 typing guarantee the pause used to buy.

    Merged in #1215 — was test_chat_mode_routes_message_to_chief_not_dispatch:
    Chat mode send = ensure → input proxy ({data, submit:true}); the
    one-shot /api/board/dispatch is never touched; the box clears
    (conversation semantics, unlike dispatch's keep-for-multi-dispatch).
    Its checks run right after the send, before the payload swap below."""
    board = {"body": _json.dumps(_board_payload(with_chief=True))}
    authed_page.route(
        re.compile(r".*/api/board(?:\?.*)?$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=board["body"],
        ),
    )
    _mock_board_side_routes(authed_page)
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

    # -- was test_chat_mode_routes_message_to_chief_not_dispatch --
    # Chat mode: the model select greys out (chief model is owned by chief
    # settings) and the status row appears.
    expect(
        authed_page.locator("#boardDispatchModel .model-combo-trigger")
    ).to_be_disabled()
    expect(authed_page.locator("#boardChiefStatus")).to_be_visible()

    # (The message text is that test's, so its input-body assertion stands
    # verbatim; this test never asserted on its own send text.)
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

    # -- #958: the poll keeps landing while the chief drawer stays open --
    drawer = authed_page.locator("li.board-item-chief .board-drawer")
    expect(drawer).to_be_visible()
    reply = drawer.locator(".board-drawer-composer .composer-input")
    reply.fill("half-typed follow-up")
    # Tag the live node: a drawer rebuilt by the poll loses the tag even
    # when it re-opens looking identical.
    drawer.evaluate("el => { el.dataset.e2eTag = 'pre-poll'; }")

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
    board["body"] = _json.dumps(changed)

    # Two poll intervals (BOARD_POLL_MS is 5 s) of margin for a loaded box.
    expect(
        authed_page.locator('.board-list[data-col="claude_turn"]')
    ).to_contain_text("fresh lane", timeout=12_000)
    expect(
        authed_page.locator('.board-list[data-col="done"]')
    ).to_contain_text("closed while chatting")

    expect(drawer).to_have_attribute("data-e2e-tag", "pre-poll")
    expect(reply).to_have_value("half-typed follow-up")
    expect(reply).to_be_focused()


# ------------------------------------------- chief's answer sheet (#1295)

# The chief's questions as its plan writer records them (additive plan v1),
# served as the real reader (src/chief_plan.py) makes of them: one full
# single-select, one multi-select with no ref whose recommendation marks no
# option, one old-style text-only item.
_QUESTIONS = [
    {"id": "q-plans", "text": "Approve the plans", "ref": "fleet-config#959", "repo": "fleet-config",
     "question": "Approve the four plans?", "detail": "Each plan ships as its own PR.",
     "recommendation": "Yes: all four are small and independent.",
     "options": [
         {"label": "Yes, all four", "description": "Ship them in order", "recommended": True},
         {"label": "Only the first"},
         {"label": "Hold them"},
     ]},
    {"id": "q-days", "text": "Which days?", "repo": "life-os", "question": "Which days work?",
     "multi": True, "recommendation": "Mon and Wed suit the gym.",
     "options": [{"label": "Mon"}, {"label": "Wed"}, {"label": "Fri"}]},
    {"text": "Remember the last tab?", "ref": "app-launcher#1131"},
]


def _plan_with(tmp_path, waiting: list) -> dict:
    f = tmp_path / "chief-plan.json"
    f.write_text(_json.dumps({
        "version": 1, "updated_at": "2026-09-27T09:00:00Z", "lanes": [],
        "queue": [{"repo": "app-launcher", "ref": "#1295", "title": "answer sheet", "status": "building"}],
        "waiting_on_roberto": waiting,
    }), encoding="utf-8")
    return chief_plan.read_chief_plan(f, "octo")


def _route_plan_from(page: Page, current: dict) -> None:
    """Serve ``current["plan"]`` from the chief-plan endpoint at request time
    (registered after _mock_board, so it wins over that helper's fixed plan)."""
    page.route(
        re.compile(r".*/api/board/chief-plan$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(current["plan"]),
        ),
    )


# A 1x1 PNG, named `e2e-stub-...` for the conftest upload-leak check (#922).
_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk"
    "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)
_CHIEF_UPLOAD = "E:/automation/fleet-config/.launcher-tmp/e2e-stub-paste.png"


def _mock_chief_upload(page: Page) -> list:
    """Stub the chief session's attachment route, as Chat's composer uploads
    to its session's; return the requested URLs."""
    uploads: list = []

    def _capture(route):
        uploads.append(route.request.url)
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"path": _CHIEF_UPLOAD}),
        )

    page.route(re.compile(r".*/api/claude-code/sessions/s-chief/image(?:\?.*)?$"), _capture)
    return uploads


def _paste_image(page: Page, target: str) -> bool:
    """Fire a synthetic image paste on ``target``, as the Chat composer's
    #1206 test does; return whether the page took it (defaultPrevented)."""
    return page.evaluate(
        """([b64, target]) => {
          const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
          const dt = new DataTransfer();
          dt.items.add(new File([bytes], 'e2e-stub-paste.png', {type: 'image/png'}));
          const ev = new Event('paste', {bubbles: true, cancelable: true});
          Object.defineProperty(ev, 'clipboardData', {value: dt});
          document.querySelector(target).dispatchEvent(ev);
          return ev.defaultPrevented;
        }""",
        [base64.b64encode(_PNG_1x1).decode(), target],
    )


def _capture_chief_input(page: Page) -> list:
    posts: list = []

    def _capture(route):
        posts.append(route.request.post_data_json)
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"ok": True, "bytes": 8, "submit": True}),
        )

    page.route(re.compile(r".*/api/claude-code/sessions/s-chief/input$"), _capture)
    return posts


@pytest.mark.iphone
def test_answer_button_shows_only_with_questions(
    authed_page: Page, base_url: str, tmp_path
) -> None:
    """#1295: the Chief's-plan card offers "Answer N questions" only when
    something waits on Roberto; with nothing waiting there is no button."""
    _mock_board(authed_page, _board_payload(with_chief=True))
    current = {"plan": _plan_with(tmp_path, [])}
    _route_plan_from(authed_page, current)
    _open_board(authed_page, base_url)
    plan_body = authed_page.locator("#boardChiefPlan .board-plan-body")
    expect(plan_body).to_have_attribute("data-state", "ok")
    expect(plan_body.locator(".board-plan-answer")).to_have_count(0)

    current["plan"] = _plan_with(tmp_path, _QUESTIONS)
    authed_page.locator("#boardRefresh").click()
    button = plan_body.locator(".board-plan-answer")
    expect(button).to_have_text("Answer 3 questions")
    # The card lists every item, an old text-only one included.
    expect(plan_body.locator("li.board-plan-waiting")).to_have_count(3)
    expect(plan_body.locator("li.board-plan-waiting").nth(2)).to_contain_text("Remember the last tab?")
    box = stable_read(button.bounding_box)
    assert box and box["height"] >= 44, f"answer button under the 44px tap floor: {box}"


@pytest.mark.iphone
def test_answer_sheet_renders_questions_and_done_sends_one_message(
    authed_page: Page, base_url: str, tmp_path
) -> None:
    """#1295: the sheet shows each question in file order (repo, linked ref,
    question, detail, the options with the recommended one labelled but not
    picked, Other on every item, an old text-only item as a free-text
    question) plus Anything else. The recommendation is no card and no star:
    the labelled option carries it, and an item whose recommendation marks
    no option keeps it as one quiet line. Anything else is the shared
    composer (mic + image, no Send of its own), and a pasted image uploads to
    the chief's session and rides the message as its path, as a Chat send
    carries it. Done sends exactly one message, composed from every answer,
    through the chat bar's ensure → input path; the sheet closes, a toast
    says so, and the card marks what this viewer answered."""
    _mock_board(authed_page, _board_payload(with_chief=True))
    _route_plan_from(authed_page, {"plan": _plan_with(tmp_path, _QUESTIONS)})
    ensured: dict = {}
    _mock_ensure(authed_page, ensured)
    posts = _capture_chief_input(authed_page)
    uploads = _mock_chief_upload(authed_page)
    _open_board(authed_page, base_url)

    authed_page.locator("#boardChiefPlan .board-plan-answer").click()
    dialog = authed_page.locator("#chiefAnswersDialog")
    expect(dialog).to_be_visible()
    blocks = dialog.locator(".chief-answer")
    expect(blocks).to_have_count(3)

    first = blocks.nth(0)
    expect(first.locator(".chief-answer-repo")).to_have_text("fleet-config")
    expect(first.locator("a.chief-answer-ref")).to_have_attribute(
        "href", "https://github.com/octo/fleet-config/issues/959")
    expect(first.locator(".tr-ask-question")).to_have_text("Approve the four plans?")
    expect(first.locator(".chief-answer-detail")).to_have_text("Each plan ships as its own PR.")
    # The recommended option says so itself: no separate block, no star.
    expect(first.locator(".chief-answer-rec")).to_have_count(0)
    expect(dialog.locator('use[href="#i-star"]')).to_have_count(0)
    options = first.locator(".tr-ask-opt")
    expect(options).to_have_count(3)
    expect(options.nth(0)).to_contain_text("Yes, all four (Recommended)")
    expect(options.nth(0)).to_contain_text("Ship them in order")
    expect(first.locator('.tr-ask-opt[aria-pressed="true"]')).to_have_count(0)

    second = blocks.nth(1)
    expect(second.locator(".chief-answer-ref")).to_have_count(0)
    expect(second.get_by_text("Pick any number")).to_be_visible()
    # A recommendation no option carries stays, as one quiet line of text.
    rec = second.locator(".chief-answer-rec")
    expect(rec).to_have_text("Recommended: Mon and Wed suit the gym.")
    expect(rec).to_have_class(re.compile(r"\btr-ask-hint\b"))
    expect(rec.locator("svg")).to_have_count(0)

    third = blocks.nth(2)
    expect(third.locator(".tr-ask-question")).to_have_text("Remember the last tab?")
    expect(third.locator(".tr-ask-opt")).to_have_count(0)
    expect(third.locator("a.chief-answer-ref")).to_have_attribute(
        "href", "https://github.com/octo/app-launcher/issues/1131")

    expect(dialog.locator(".tr-ask-input")).to_have_count(3)  # Other on every item
    expect(third.locator(".tr-ask-input")).to_have_attribute("placeholder", "Your answer")
    # Anything else is the shared composer Chat and Terminal mount: its mic
    # and image controls, but not its Send (Done is the one send) or keys.
    also = dialog.locator("#chiefAnswersAlso")
    expect(also).to_have_class(re.compile(r"\bcomposer\b"))
    expect(also.locator(".composer-input")).to_be_visible()
    expect(also.locator(".composer-mic")).to_be_visible()
    expect(also.locator(".composer-image")).to_be_visible()
    expect(also.locator(".composer-send")).to_be_hidden()
    expect(also.locator(".composer-keys")).to_be_hidden()

    # Nothing answered yet: Done waits, and says why.
    done = dialog.locator("#chiefAnswersDone")
    expect(done).to_be_disabled()
    expect(dialog.locator("#chiefAnswersNote")).to_contain_text("Pick or type an answer first")

    # Single-select: typing Other then tapping an option keeps only the pick.
    first.locator(".tr-ask-input").fill("maybe later")
    options.nth(0).click()
    expect(options.nth(0)).to_have_attribute("aria-pressed", "true")
    expect(first.locator(".tr-ask-input")).to_have_value("")
    # Multi-select: any number of picks, plus Other.
    second.locator(".tr-ask-opt").nth(0).click()
    second.locator(".tr-ask-opt").nth(1).click()
    second.locator(".tr-ask-input").fill("not   Friday")
    # The third is skipped.
    also_text = also.locator(".composer-input")
    also_text.fill("ship it tonight")
    expect(done).to_be_enabled()
    # A pasted image uploads to the chief's session, and its path lands in
    # the box as its own paragraph, exactly as the Chat composer appends it.
    assert _paste_image(authed_page, "#chiefAnswersAlso .composer-input")
    expect(also_text).to_have_value("ship it tonight\n\n" + _CHIEF_UPLOAD)
    assert len(uploads) == 1 and "inline=1" in uploads[0], uploads

    viewport = authed_page.viewport_size
    box = dialog.bounding_box()
    assert box, "answer sheet not laid out"
    if viewport["width"] <= 520:
        assert abs(box["width"] - viewport["width"]) < 2 and box["height"] >= viewport["height"] - 2, (
            f"phone sheet should be full-screen: {box} vs {viewport}")
    else:
        assert box["width"] <= 440, f"desktop sheet should stay a centred dialog: {box}"

    done.click()
    expect(authed_page.locator("#toast")).to_contain_text("Sent to chief")
    expect(dialog).to_be_hidden()
    assert ensured.get("body", {}).get("fresh") is not True, "answers must never restart the chief"
    # #1351: answers must keep the live chief that asked.
    assert ensured["body"].get("restart") is not True, "answers must keep a live chief"
    assert posts == [{
        "data": (
            "Answers from the Board (2 of 3):\n"
            "1. [fleet-config#959] Approve the four plans? → Yes, all four (recommended) {id: q-plans}\n"
            "2. [life-os] Which days work? → Mon, Wed; Other: not Friday {id: q-days}\n"
            "Skipped: 3\n"
            "Also: ship it tonight\n\n" + _CHIEF_UPLOAD
        ),
        "submit": True,
    }], posts

    rows = authed_page.locator("#boardChiefPlan li.board-plan-waiting")
    expect(rows.nth(0)).to_contain_text("answered, waiting for the chief")
    expect(rows.nth(1)).to_contain_text("answered, waiting for the chief")
    expect(rows.nth(2)).not_to_contain_text("answered")


def test_repo_filter_narrows_the_plan_and_its_questions(
    authed_page: Page, base_url: str, tmp_path
) -> None:
    """#1332: the Dispatch repo picker narrows the Chief's plan as it does
    the columns. Waiting on you, Lanes and Queue keep only the picked repo's
    rows (a repo from `repo`, or the repo half of a `repo#N` ref) plus rows
    with no repo, which are fleet-wide and never hidden; a note counts the
    questions the filter hides; a group the filter empties is gone. "Answer
    N questions" counts and opens only what shows, the message numbers each
    by its place in the whole plan and doesn't list hidden ones as skipped,
    and clearing the filter restores everything."""
    authed_page.route(
        re.compile(r".*/api/apps$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"scan_root": "", "apps": [
                {"id": "cc-" + name, "kind": "claude-code", "name": name,
                 "project_dir": "E:/automation/" + name}
                for name in ("app-launcher", "fleet-config", "life-os")
            ]}),
        ),
    )
    _mock_board(authed_page, _board_payload(with_chief=True))
    f = tmp_path / "chief-plan.json"
    f.write_text(_json.dumps({
        "version": 1, "updated_at": "2026-09-27T09:00:00Z",
        "lanes": [
            {"repo": "app-launcher", "session": "s-a", "item": "#1295", "status": "building"},
            {"repo": "fleet-config", "session": "s-f", "item": "#959", "status": "gate"},
        ],
        "queue": [
            {"repo": "app-launcher", "ref": "#1295", "title": "answer sheet", "status": "building"},
            {"ref": "fleet-config#900", "title": "hooks sweep", "status": "someday"},
        ],
        # fleet-config (repo + ref), life-os (repo only), app-launcher (ref
        # only), then one with no repo at all.
        "waiting_on_roberto": _QUESTIONS + [
            {"id": "q-pause", "text": "Pause every lane tonight?"},
        ],
    }), encoding="utf-8")
    _route_plan_from(authed_page, {"plan": chief_plan.read_chief_plan(f, "octo")})
    ensured: dict = {}
    _mock_ensure(authed_page, ensured)
    posts = _capture_chief_input(authed_page)
    _open_board(authed_page, base_url)

    plan_body = authed_page.locator("#boardChiefPlan .board-plan-body")
    waiting = plan_body.locator("li.board-plan-waiting")
    headings = plan_body.locator(".board-plan-heading")
    button = plan_body.locator(".board-plan-answer")
    hidden_note = plan_body.locator(".board-plan-hidden")
    expect(waiting).to_have_count(4)
    expect(button).to_have_text("Answer 4 questions")
    expect(hidden_note).to_have_count(0)

    def pick(repo: str) -> None:
        authed_page.locator("#boardDispatchRepoBtn").click()
        item = authed_page.locator(f'#boardDispatchRepoList li[data-repo="{repo}"]')
        expect(item).to_be_visible(timeout=15_000)
        item.click()

    pick("app-launcher")
    expect(waiting).to_have_count(2)
    expect(waiting.nth(0)).to_contain_text("Remember the last tab?")
    expect(waiting.nth(1)).to_contain_text("Pause every lane tonight?")
    expect(button).to_have_text("Answer 2 questions")
    expect(hidden_note).to_have_text("2 more questions in other projects")
    expect(headings).to_have_text(["Waiting on you", "Lanes", "Queue"])
    expect(plan_body.locator(".board-plan-group").nth(1).locator("li")).to_have_count(1)
    expect(plan_body.locator(".board-plan-group").nth(2).locator("li")).to_have_count(1)
    expect(plan_body.locator(".board-plan-group").nth(2)).to_contain_text("answer sheet")

    # No lane or queue row is life-os's, so both groups go, not left empty.
    pick("life-os")
    expect(waiting).to_have_count(2)
    expect(waiting.nth(0)).to_contain_text("Which days?")
    expect(headings).to_have_text(["Waiting on you"])
    expect(hidden_note).to_have_text("2 more questions in other projects")

    # The sheet asks only what shows, each numbered by its place in the plan.
    pick("app-launcher")
    button.click()
    dialog = authed_page.locator("#chiefAnswersDialog")
    blocks = dialog.locator(".chief-answer")
    expect(blocks).to_have_count(2)
    expect(blocks.nth(0).locator(".chief-answer-num")).to_have_text("3")
    expect(blocks.nth(1).locator(".chief-answer-num")).to_have_text("4")
    blocks.nth(0).locator(".tr-ask-input").fill("yes")
    dialog.locator("#chiefAnswersDone").click()
    expect(dialog).to_be_hidden()
    assert posts == [{
        "data": (
            "Answers from the Board (1 of 2):\n"
            "3. [app-launcher#1131] Remember the last tab? → Other: yes\n"
            "Skipped: 4"
        ),
        "submit": True,
    }], posts

    # All projects again: the whole plan, and the answered mark on its row.
    pick("")
    expect(waiting).to_have_count(4)
    expect(button).to_have_text("Answer 4 questions")
    expect(hidden_note).to_have_count(0)
    expect(headings).to_have_text(["Waiting on you", "Lanes", "Queue"])
    expect(waiting.nth(2)).to_contain_text("answered, waiting for the chief")
    expect(waiting.nth(0)).not_to_contain_text("answered")


@pytest.mark.iphone
def test_answer_sheet_done_disabled_when_no_chief(
    authed_page: Page, base_url: str, tmp_path
) -> None:
    """#1295: with no chief running there is nobody to send to — Done is
    disabled with a plain note, answers typed or not, and nothing is sent."""
    _mock_board(authed_page, _board_payload(with_chief=False))
    _route_plan_from(authed_page, {"plan": _plan_with(tmp_path, _QUESTIONS)})
    posts = _capture_chief_input(authed_page)
    _open_board(authed_page, base_url)

    authed_page.locator("#boardChiefPlan .board-plan-answer").click()
    dialog = authed_page.locator("#chiefAnswersDialog")
    dialog.locator(".chief-answer").nth(2).locator(".tr-ask-input").fill("yes")
    expect(dialog.locator("#chiefAnswersDone")).to_be_disabled()
    expect(dialog.locator("#chiefAnswersNote")).to_have_text("Chief not running.")
    dialog.locator("#chiefAnswersClose").click()
    expect(dialog).to_be_hidden()
    assert posts == []
