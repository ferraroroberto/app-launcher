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
    authed_page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=10_000)
    expect(authed_page.locator("#terminalTitle .terminal-title-crown")).to_have_count(1)


def test_chief_stop_requires_confirm_worker_row_does_not(
    authed_page: Page, base_url: str
) -> None:
    """Parity with the Board drawer's own guard (#245/board.js:302, mirrored
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

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    chief_row = authed_page.locator(
        '#sessionsList li[data-session-id="s-chief"]'
    )
    worker_row = authed_page.locator(
        '#sessionsList li[data-session-id="s-work"]'
    )

    dialogs: list[str] = []

    # 1. Chief + dismiss -> stop never fires.
    authed_page.once("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    chief_row.locator(".action-stop-close").click()
    authed_page.wait_for_timeout(400)
    assert len(dialogs) == 1 and "chief" in dialogs[0].lower()
    assert stops == [], "dismissing the confirm must not stop the chief"

    # 2. Chief + accept -> stop fires.
    authed_page.once("dialog", lambda d: (dialogs.append(d.message), d.accept()))
    chief_row.locator(".action-stop-close").click()
    authed_page.wait_for_timeout(600)
    assert len(dialogs) == 2
    assert len(stops) == 1 and "/sessions/s-chief/stop" in stops[0]["url"]

    # 3. Worker row -> one-tap stop, no dialog. (An unexpected confirm would
    # be auto-dismissed by Playwright and show up as a missing stop call.)
    worker_row.locator(".action-stop-close").click()
    authed_page.wait_for_timeout(600)
    assert len(dialogs) == 2, "worker row must not raise a confirm dialog"
    assert len(stops) == 2 and "/sessions/s-work/stop" in stops[1]["url"]


def test_coding_tab_offers_manual_start_when_chief_down(
    authed_page: Page, base_url: str
) -> None:
    _mock_sessions(authed_page, [_WORKER_SESSION])
    captured: dict = {}
    _mock_ensure(authed_page, captured, spawned=True)

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    status = authed_page.locator("#codingChiefStatus")
    expect(status).to_be_visible()
    expect(authed_page.locator("#codingChiefStatusText")).to_have_text(
        "chief: not running"
    )
    start_btn = authed_page.locator("#codingChiefStart")
    expect(start_btn).to_be_visible()

    start_btn.click()
    authed_page.wait_for_timeout(400)
    assert captured.get("method") == "POST"


def test_coding_tab_hides_start_when_chief_alive(
    authed_page: Page, base_url: str
) -> None:
    _mock_sessions(authed_page, [_CHIEF_SESSION, _WORKER_SESSION])
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    expect(authed_page.locator("#codingChiefStatus")).to_be_visible()
    expect(authed_page.locator("#codingChiefStatusText")).to_have_text(
        "chief: running"
    )
    expect(authed_page.locator("#codingChiefStart")).to_be_hidden()
