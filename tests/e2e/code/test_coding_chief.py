"""Coding-tab / terminal-overlay parity with the Board's fleet-chief handling
(issue #547, follow-up to #245).

Before this issue the chief's crown, kill-confirm, and manual Start
affordance only existed in the Board tab's chat-mode UI — the Coding tab's
session list and the terminal overlay had no idea a given running session
was the chief, so it could be killed there with zero confirmation and there
was no way to spot or (re)start it without switching to Board chat mode.

Hermetic like ``board/test_chief_card.py``: ``GET /api/claude-code/sessions`` is
route-mocked before ``goto`` with a chief + a worker session (the
``PtySession.to_api()`` shape — distinct from the Board's own card shape
used in ``board/_chief_support.py``), so this pins the frontend contract without
depending on the real session-host.
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._geometry import assert_no_overlap
from tests.e2e.fixtures.ui import open_row_menu, open_session_row, stub_session_mirror
from tests.e2e.fixtures.waits import (
    OVERLAY_OPEN_MS,
    assert_unpainted_at_rest,
    flush_requests,
    wait_until,
)

pytestmark = pytest.mark.smoke

_CHIEF_SESSION = {
    "session_id": "s-chief", "kind": "pty", "agent": "claude",
    "label": "chief", "project_dir": "E:/automation/fleet-config",
    "name": "chief", "flags": "", "started_at": "2026-07-19T06:00:00Z",
    "alive": True, "rows": 40, "cols": 120,
    "live_title": "", "prompt_title": "", "manual_title": "chief",
    "output_chars": 1200,
}

_WORKER_SESSION = {
    "session_id": "s-work", "kind": "pty", "agent": "claude",
    "label": "", "project_dir": "E:/automation/life-os",
    "name": "life-os", "flags": "", "started_at": "2026-07-19T06:30:00Z",
    "alive": True, "rows": 40, "cols": 120,
    "live_title": "weekly recap", "prompt_title": "", "manual_title": "",
    "output_chars": 900,
}


def _mock_sessions(page: Page, sessions: list[dict]) -> None:
    page.route(
        re.compile(r".*/api/claude-code/sessions$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"sessions": sessions}),
        ),
    )


def _mock_ensure(page: Page, captured: dict, *, spawned: bool = True) -> None:
    def _capture(route):
        captured["method"] = route.request.method
        captured["body"] = route.request.post_data_json
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"session_id": "s-chief", "spawned": spawned}),
        )
    page.route(re.compile(r".*/api/board/chief/ensure$"), _capture)


def _restart_chief(page: Page, captured: dict, toast: str) -> None:
    """Restart from the running chief's kebab: the themed confirm, then one
    ensure POST with fresh:true AND resume:true, and the toast names the
    outcome (#617 / #649, moved from the Board's chief card in #1436)."""
    captured.clear()
    row = page.locator('#sessionsList li[data-session-id="s-chief"]')
    row.locator(".session-kebab").click()
    restart = open_row_menu(page).locator("[data-item=chief-restart-btn]")
    expect(restart).to_be_visible()
    restart.click()
    # Restart asks in the themed confirm (#1501), same words as before.
    expect(page.locator("#confirmDialogTitle")).to_have_text("Restart the chief?")
    expect(page.locator("#confirmDialogMessage")).to_contain_text(
        "stop the current one gracefully")
    page.locator("#confirmDialogOk").click()
    wait_until(page, lambda: captured.get("method") == "POST", "Restart's ensure POST")
    assert captured["body"].get("fresh") is True, captured["body"]
    assert captured["body"].get("resume") is True, captured["body"]
    expect(page.locator("#toast")).to_contain_text(toast)


def test_a_running_chief_is_crowned_its_kebab_restarts_it_and_a_worker_has_neither(
    authed_page: Page, base_url: str
) -> None:
    """A running chief's row is crowned and carries no Start / Resume; Restart
    and Chief settings sit in its kebab, before Stop, which stays last (#1434).
    Restart is a graceful stop-then-resume of the same conversation, never the
    session-host restart; with nothing resumable it degrades to a fresh spawn
    and the toast says so. The worker's row has no crown and neither item."""
    _mock_sessions(authed_page, [_CHIEF_SESSION, _WORKER_SESSION])
    captured: dict = {}
    outcome = {"resumed": True, "resume_fallback_reason": ""}

    def _ensure(route):
        captured["method"] = route.request.method
        captured["body"] = route.request.post_data_json
        route.fulfill(status=200, content_type="application/json", body=_json.dumps({
            "session_id": "s-chief", "spawned": True, **outcome,
        }))

    authed_page.route(re.compile(r".*/api/board/chief/ensure$"), _ensure)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    chief_row = authed_page.locator('#sessionsList li[data-session-id="s-chief"]')
    worker_row = authed_page.locator('#sessionsList li[data-session-id="s-work"]')
    expect(chief_row).to_have_class(re.compile(r"session-item-chief"))
    expect(chief_row.locator(".board-chief-crown")).to_have_count(1)
    expect(worker_row).not_to_have_class(re.compile(r"session-item-chief"))
    expect(worker_row.locator(".board-chief-crown")).to_have_count(0)
    expect(authed_page.locator("#codingChiefStart")).to_have_count(0)
    expect(authed_page.locator("#codingChiefResume")).to_have_count(0)

    chief_row.locator(".session-kebab").click()
    items = open_row_menu(authed_page).locator(".row-menu-item")
    expect(open_row_menu(authed_page).locator("[data-item=chief-restart-btn]")).to_be_visible()
    expect(open_row_menu(authed_page).locator("[data-item=chief-settings-btn]")).to_be_visible()
    expect(items.last).to_have_attribute("data-item", "action-stop-close")

    # The worker's menu has neither.
    authed_page.keyboard.press("Escape")
    worker_row.locator(".session-kebab").click()
    expect(open_row_menu(authed_page).locator("[data-item=action-stop-close]")).to_be_visible()
    expect(open_row_menu(authed_page).locator("[data-item=chief-restart-btn]")).to_have_count(0)
    expect(open_row_menu(authed_page).locator("[data-item=chief-settings-btn]")).to_have_count(0)
    authed_page.keyboard.press("Escape")

    _restart_chief(authed_page, captured, "Chief resumed")
    outcome.update(resumed=False,
                   resume_fallback_reason="no resumable chief conversation found in the last 24h")
    _restart_chief(authed_page, captured, "No resumable conversation")


@pytest.mark.iphone
def test_chief_terminal_overlay_shows_crown_in_title(
    authed_page: Page, base_url: str, browser_name: str
) -> None:
    # A row tap opens the in-page terminal only on a touch (phone) client —
    # a desktop browser opens a dedicated PC mirror window instead (#282),
    # same split test_stop_unify_and_terminal_kill.py's kill test observes.
    if browser_name != "webkit":
        pytest.skip(
            "in-page terminal view via row-tap is phone-only since #282; the "
            "desktop row-tap opens a mirror window"
        )
    _mock_sessions(authed_page, [_CHIEF_SESSION, _WORKER_SESSION])
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    authed_page.locator(
        '#sessionsList li[data-session-id="s-chief"] .session-open'
    ).click()
    authed_page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)
    expect(authed_page.locator("#terminalTitle .terminal-title-crown")).to_have_count(1)


def test_chief_stop_requires_confirm_worker_row_does_not(
    authed_page: Page, base_url: str
) -> None:
    """Parity with the Board drawer's own guard (#245, the isChiefCard()
    check on the stop button in board/drawer.js::buildDrawer, mirrored
    here via the shared isChiefSession() predicate, #547): the chief's stop
    button asks first (dismiss -> no stop, accept -> stop); the worker row
    keeps the deliberate one-tap stop (#253) with no dialog at all."""
    _mock_sessions(authed_page, [_CHIEF_SESSION, _WORKER_SESSION])

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
    # Both rows are full-control, so a desktop-projection tap would open a PC
    # mirror window instead of the overlay (#282).
    stub_session_mirror(authed_page)

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    chief_row = authed_page.locator(
        '#sessionsList li[data-session-id="s-chief"]'
    )
    worker_row = authed_page.locator(
        '#sessionsList li[data-session-id="s-work"]'
    )

    dialogs: list[str] = []
    overlay = authed_page.locator("#terminalOverlay")

    def _menu_stop() -> None:
        """Tap Stop in the open overlay's ⋮ menu — since #1025 the row has no
        gear of its own, so this is where a session's stop is reached from
        the Coding tab (the Board drawer's own button is the other path)."""
        authed_page.locator("#terminalMenu").click()
        menu = authed_page.locator(".terminal-menu")
        expect(menu).to_be_visible()
        menu.locator("[data-item=action-stop-close]").click()

    # 1. Chief + dismiss -> stop never fires. The overlay stays open, so
    # step 2 reuses it rather than reopening.
    # The ask is the themed confirm (#1501), never a native one.
    authed_page.on("dialog", lambda d: dialogs.append(d.message))
    confirm = authed_page.locator("#confirmDialog")
    open_session_row(authed_page, chief_row)
    _menu_stop()
    expect(confirm).to_be_visible()
    expect(authed_page.locator("#confirmDialogTitle")).to_have_text("Kill the chief session?")
    authed_page.locator("#confirmDialogClose").click()
    expect(confirm).to_be_hidden()
    flush_requests(authed_page)
    assert stops == [], "dismissing the confirm must not stop the chief"
    expect(overlay).to_be_visible()

    # 2. Chief + the action -> stop fires, and the overlay showing it closes.
    _menu_stop()
    expect(confirm).to_be_visible()
    authed_page.locator("#confirmDialogOk").click()
    wait_until(authed_page, lambda: len(stops) >= 1, "the confirmed chief stop POST")
    assert len(stops) == 1 and "/sessions/s-chief/stop" in stops[0]["url"]
    expect(overlay).to_be_hidden()

    # 3. Worker row -> one-tap stop, no dialog.
    open_session_row(authed_page, worker_row)
    _menu_stop()
    wait_until(authed_page, lambda: len(stops) >= 2, "the worker stop POST")
    flush_requests(authed_page)
    expect(confirm).to_be_hidden()
    assert len(stops) == 2 and "/sessions/s-work/stop" in stops[1]["url"]
    assert dialogs == [], f"a native dialog was raised: {dialogs}"


def test_chief_row_is_pinned_first(authed_page: Page, base_url: str) -> None:
    """#1434: the chief's row leads the Sessions list whatever order the
    session-host lists it in."""
    _mock_sessions(authed_page, [_WORKER_SESSION, _CHIEF_SESSION])
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    rows = authed_page.locator("#sessionsList > li")
    expect(rows).to_have_count(2)
    expect(rows.first).to_have_attribute("data-session-id", "s-chief")


def test_a_stopped_chief_row_offers_start_resume_and_its_settings(
    authed_page: Page, base_url: str
) -> None:
    """#1434 (was #547's separate status line): with no chief running, the
    chief's own row leads the list, crowned, with Start and Resume on it.
    Chief settings in its kebab opens Settings with the Chief sheet open and
    filled from GET /api/config (#1435; it opened the Board's own dialog
    before). That read is patched for a deterministic worker cap; every other
    /api/config call passes through to the disposable server."""
    _mock_sessions(authed_page, [_WORKER_SESSION])
    captured: dict = {}
    _mock_ensure(authed_page, captured, spawned=True)

    def _config(route):
        if route.request.method != "GET":
            route.continue_()
            return
        body = route.fetch().json()
        body.update(
            chief_model="fable", chief_worker_cap=2,
            chief_worker_cap_min=1, chief_worker_cap_max=10,
        )
        route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(body),
        )

    authed_page.route(re.compile(r".*/api/config$"), _config)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    first = authed_page.locator("#sessionsList > li").first
    expect(first).to_have_class(re.compile(r"session-item-chief"))
    expect(first.locator(".board-chief-crown")).to_have_count(1)
    expect(first.locator(".srow-meta-text")).to_have_text("stopped")
    start_btn = authed_page.locator("#codingChiefStart")
    resume_btn = authed_page.locator("#codingChiefResume")
    expect(start_btn).to_be_visible()
    expect(resume_btn).to_be_visible()
    # Plain icon-button verbs (fleet-config#1420): named, not painted.
    expect(start_btn).to_have_attribute("aria-label", "Start chief")
    expect(resume_btn).to_have_attribute("aria-label", "Resume chief")
    for verb in (start_btn, resume_btn):
        expect(verb).to_have_class(re.compile(r"\bicon-button\b"))
        assert_unpainted_at_rest(authed_page, verb)
    # Start's target and the worker's row below never share a pixel
    # (TOUCH-02, #1238).
    expect(authed_page.locator("#sessionsList .session-open")).to_have_count(1)
    assert_no_overlap([
        start_btn, resume_btn,
        authed_page.locator('#sessionsList li[data-session-id="s-work"] .session-open'),
    ])

    start_btn.click()
    wait_until(authed_page, lambda: captured.get("method") == "POST",
               "the Start button's ensure POST")
    assert not captured["body"].get("resume"), captured["body"]

    captured.clear()
    resume_btn.click()
    wait_until(authed_page, lambda: captured.get("method") == "POST",
               "the Resume button's ensure POST")
    assert captured["body"].get("resume") is True, captured["body"]
    assert captured["body"].get("restart") is True, captured["body"]

    first.locator(".session-kebab").click()
    menu = open_row_menu(authed_page)
    expect(menu.locator("[data-item=chief-settings-btn]")).to_be_visible()
    # Stopped: nothing to restart.
    expect(menu.locator("[data-item=chief-restart-btn]")).to_have_count(0)
    menu.locator("[data-item=chief-settings-btn]").click()
    expect(authed_page.locator("#paneSettings")).to_be_visible()
    expect(authed_page.locator("#chiefSheet")).to_be_visible()
    expect(authed_page.locator("#chiefSettingsDialog")).to_have_count(0)
    expect(authed_page.locator("#chiefWorkerCap")).to_have_value("2")


# -- #1436: the Board has no chief card, so Code › Sessions is the chief's
# one home. Sending it a message and restarting it are pinned here.

def _chief_transcript() -> dict:
    return {
        "available": True, "source": "native", "reason": None, "session_id": "s-chief",
        "next_cursor": None,
        "entries": [
            {"kind": "user", "timestamp": "2026-07-19T07:00:00Z",
             "text": "what's open?", "truncated": False, "sidechain": False},
            {"kind": "assistant", "timestamp": "2026-07-19T07:00:05Z",
             "text": "Six open issues.", "truncated": False, "sidechain": False},
        ],
    }


@pytest.mark.iphone
def test_chief_row_opens_chat_whose_composer_reaches_the_chief(
    authed_page: Page, base_url: str
) -> None:
    """#1436: with the Board's chief composer gone, a message to the chief
    goes from its Code row: a tap opens the session view, whose Chat mode
    carries the shared composer (dictation included), and Send rides the
    chief's own /input route."""
    _mock_sessions(authed_page, [_CHIEF_SESSION, _WORKER_SESSION])
    authed_page.route(
        re.compile(r".*/api/claude-code/sessions/s-chief/transcript(\?.*)?$"),
        lambda route: route.fulfill(status=200, content_type="application/json",
                                    body=_json.dumps(_chief_transcript())),
    )
    captured: dict = {}

    def _input(route):
        captured["body"] = route.request.post_data_json
        route.fulfill(status=200, content_type="application/json", body=_json.dumps({
            "ok": True, "bytes": 8, "submit": True, "delivered": True, "reason": "confirmed",
            "ingested": True, "submitted": True, "submit_confirmed": True,
            "submit_state": "confirmed", "waited_ms": 40, "deferred": False,
        }))

    authed_page.route(re.compile(r".*/api/claude-code/sessions/s-chief/input$"), _input)
    stub_session_mirror(authed_page)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    row = authed_page.locator('#sessionsList li[data-session-id="s-chief"]')
    expect(row).to_be_visible()
    open_session_row(authed_page, row, mode="chat")
    expect(authed_page.locator("#transcriptList")).to_contain_text("Six open issues.")
    expect(authed_page.locator("#chatComposeBar .composer-mic")).to_have_count(1)
    field = authed_page.locator("#chatComposeBar .composer-input")
    field.fill("ok start 229")
    authed_page.locator("#chatComposeBar .composer-send").click()
    wait_until(authed_page, lambda: captured.get("body") is not None, "the chief's input POST")
    assert captured["body"] == {"data": "ok start 229", "submit": True}
    expect(field).to_have_value("")
