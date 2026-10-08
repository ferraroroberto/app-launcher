"""Coding-tab / terminal-overlay parity with the Board's fleet-chief handling
(issue #547, follow-up to #245).

Before this issue the chief's crown, kill-confirm, and manual Start
affordance only existed in the Board tab's chat-mode UI — the Coding tab's
session list and the terminal overlay had no idea a given running session
was the chief, so it could be killed there with zero confirmation and there
was no way to spot or (re)start it without switching to Board chat mode.

Hermetic like ``test_board_chief.py``: ``GET /api/claude-code/sessions`` is
route-mocked before ``goto`` with a chief + a worker session (the
``PtySession.to_api()`` shape — distinct from the Board's own card shape
used in ``test_board_chief.py``), so this pins the frontend contract without
depending on the real session-host.
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._geometry import assert_no_overlap
from tests.e2e.conftest import (
    OVERLAY_OPEN_MS,
    flush_requests,
    open_session_row,
    stub_session_mirror,
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


def test_chief_row_shows_crown_worker_row_does_not(
    authed_page: Page, base_url: str
) -> None:
    _mock_sessions(authed_page, [_CHIEF_SESSION, _WORKER_SESSION])
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    chief_row = authed_page.locator(
        '#sessionsList li[data-session-id="s-chief"]'
    )
    worker_row = authed_page.locator(
        '#sessionsList li[data-session-id="s-work"]'
    )
    expect(chief_row).to_have_class(re.compile(r"session-item-chief"))
    expect(chief_row.locator(".board-chief-crown")).to_have_count(1)
    expect(worker_row).not_to_have_class(re.compile(r"session-item-chief"))
    expect(worker_row.locator(".board-chief-crown")).to_have_count(0)


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
    check on the stop button in board.js::buildDrawer, mirrored
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
        menu = authed_page.locator("#terminalOverlay .terminal-menu")
        expect(menu).to_be_visible()
        menu.locator(".action-stop-close").click()

    # 1. Chief + dismiss -> stop never fires. The overlay stays open, so
    # step 2 reuses it rather than reopening.
    open_session_row(authed_page, chief_row)
    authed_page.once("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    _menu_stop()
    wait_until(authed_page, lambda: len(dialogs) >= 1, "the chief stop confirm")
    flush_requests(authed_page)
    assert len(dialogs) == 1 and "chief" in dialogs[0].lower()
    assert stops == [], "dismissing the confirm must not stop the chief"
    expect(overlay).to_be_visible()

    # 2. Chief + accept -> stop fires, and the overlay showing it closes.
    authed_page.once("dialog", lambda d: (dialogs.append(d.message), d.accept()))
    _menu_stop()
    wait_until(authed_page, lambda: len(stops) >= 1, "the confirmed chief stop POST")
    assert len(dialogs) == 2
    assert len(stops) == 1 and "/sessions/s-chief/stop" in stops[0]["url"]
    expect(overlay).to_be_hidden()

    # 3. Worker row -> one-tap stop, no dialog. (An unexpected confirm would
    # be auto-dismissed by Playwright and show up as a missing stop call.)
    open_session_row(authed_page, worker_row)
    _menu_stop()
    wait_until(authed_page, lambda: len(stops) >= 2, "the worker stop POST")
    flush_requests(authed_page)
    assert len(dialogs) == 2, "worker row must not raise a confirm dialog"
    assert len(stops) == 2 and "/sessions/s-work/stop" in stops[1]["url"]


def test_chief_row_is_pinned_first(authed_page: Page, base_url: str) -> None:
    """#1434: the chief's row leads the Sessions list whatever order the
    session-host lists it in."""
    _mock_sessions(authed_page, [_WORKER_SESSION, _CHIEF_SESSION])
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    rows = authed_page.locator("#sessionsList > li")
    expect(rows).to_have_count(2)
    expect(rows.first).to_have_attribute("data-session-id", "s-chief")


def test_coding_tab_offers_manual_start_when_chief_down(
    authed_page: Page, base_url: str
) -> None:
    """#1434 (was #547's separate status line): with no chief running, the
    chief's own row leads the list, crowned, with Start and Resume on it."""
    _mock_sessions(authed_page, [_WORKER_SESSION])
    captured: dict = {}
    _mock_ensure(authed_page, captured, spawned=True)

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    first = authed_page.locator("#sessionsList > li").first
    expect(first).to_have_class(re.compile(r"session-item-chief"))
    expect(first.locator(".board-chief-crown")).to_have_count(1)
    expect(first.locator(".srow-meta-text")).to_have_text("stopped")
    start_btn = authed_page.locator("#codingChiefStart")
    resume_btn = authed_page.locator("#codingChiefResume")
    expect(start_btn).to_be_visible()
    expect(resume_btn).to_be_visible()
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


def test_coding_tab_hides_start_when_chief_alive(
    authed_page: Page, base_url: str
) -> None:
    """A running chief's row carries no Start / Resume; Restart and Chief
    settings sit in its kebab, before Stop, which stays last (#1434)."""
    _mock_sessions(authed_page, [_CHIEF_SESSION, _WORKER_SESSION])
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    chief_row = authed_page.locator('#sessionsList li[data-session-id="s-chief"]')
    expect(chief_row).to_be_visible()
    expect(authed_page.locator("#codingChiefStart")).to_have_count(0)
    expect(authed_page.locator("#codingChiefResume")).to_have_count(0)

    chief_row.locator(".session-kebab").click()
    items = chief_row.locator(".session-menu button")
    expect(chief_row.locator(".chief-restart-btn")).to_be_visible()
    expect(chief_row.locator(".chief-settings-btn")).to_be_visible()
    expect(items.last).to_have_class(re.compile(r"action-stop-close"))

    # The worker's menu has neither.
    worker_row = authed_page.locator('#sessionsList li[data-session-id="s-work"]')
    authed_page.keyboard.press("Escape")
    worker_row.locator(".session-kebab").click()
    expect(worker_row.locator(".action-stop-close")).to_be_visible()
    expect(worker_row.locator(".chief-restart-btn")).to_have_count(0)
    expect(worker_row.locator(".chief-settings-btn")).to_have_count(0)


def test_chief_kebab_opens_chief_settings(authed_page: Page, base_url: str) -> None:
    """Chief settings in the chief row's kebab opens Settings with the Chief
    sheet open and filled from GET /api/config (#1435; it opened the Board's
    own dialog before). That read is patched for a deterministic worker cap;
    every other /api/config call passes through to the disposable server."""
    _mock_sessions(authed_page, [_WORKER_SESSION])

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
    first.locator(".session-kebab").click()
    expect(first.locator(".chief-restart-btn")).to_have_count(0)  # stopped: nothing to restart
    first.locator(".chief-settings-btn").click()
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


@pytest.mark.parametrize("resumed, fallback_reason, toast", [
    pytest.param(True, "", "Chief resumed", id="resumable"),
    pytest.param(
        False, "no resumable chief conversation found in the last 24h",
        "No resumable conversation", id="nothing-resumable",
    ),
])
def test_chief_row_restart_confirms_then_resumes_the_conversation(
    authed_page: Page, base_url: str, resumed: bool, fallback_reason: str, toast: str,
) -> None:
    """#617 / #649, moved from the Board's chief card (#1436): Restart in the
    running chief's kebab confirms, then POSTs ensure with fresh:true AND
    resume:true — a graceful stop-then-resume of the same conversation, never
    the session-host restart. With nothing resumable it degrades to a fresh
    spawn and the toast says so."""
    _mock_sessions(authed_page, [_CHIEF_SESSION, _WORKER_SESSION])
    captured: dict = {}

    def _ensure(route):
        captured["method"] = route.request.method
        captured["body"] = route.request.post_data_json
        route.fulfill(status=200, content_type="application/json", body=_json.dumps({
            "session_id": "s-chief", "spawned": True, "resumed": resumed,
            "resume_fallback_reason": fallback_reason,
        }))

    authed_page.route(re.compile(r".*/api/board/chief/ensure$"), _ensure)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    row = authed_page.locator('#sessionsList li[data-session-id="s-chief"]')
    row.locator(".session-kebab").click()
    restart = row.locator(".chief-restart-btn")
    expect(restart).to_be_visible()
    authed_page.once("dialog", lambda d: d.accept())
    restart.click()
    wait_until(authed_page, lambda: captured.get("method") == "POST", "Restart's ensure POST")
    assert captured["body"].get("fresh") is True, captured["body"]
    assert captured["body"].get("resume") is True, captured["body"]
    expect(authed_page.locator("#toast")).to_contain_text(toast)
