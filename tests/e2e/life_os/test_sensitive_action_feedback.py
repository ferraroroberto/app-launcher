"""Stop and Life OS Resume answer a tap at once and survive a second one.

On a slow page a tap with no visible answer gets tapped again, and the second
tap used to send the command twice. Both routes here are *held*: the request
reaches the page's route handler and waits for the test to answer it, so every
assertion before ``fulfill`` is made while the network has not yet replied.
The pending toast must already be up, a second tap must send nothing, and only
the real answer turns the toast into the outcome.

Runs on the iPhone (WebKit) projection as well as desktop; hermetic (sessions, conversations and both POSTs are route-mocked).
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e.fixtures.ui import (
    life_launch_switch,
    open_session_row,
    stub_session_mirror,
)
from tests.e2e.fixtures.waits import flush_requests, wait_until
from tests.e2e.life_os._support import (
    _mock_conversations,
    _mock_skills,
    _open_conversations,
    _open_sheet,
)

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

# The pending toast must be up before the network answers; the route is held,
# so this bound only has to outlast the render, not race the server.
_TOAST_MS = 500

_WORKER = {
    "session_id": "s-work", "kind": "pty", "agent": "claude",
    "label": "", "project_dir": "E:/automation/life-os",
    "name": "life-os", "flags": "", "started_at": "2026-07-19T06:30:00Z",
    "alive": True, "rows": 40, "cols": 120,
    "live_title": "weekly recap", "prompt_title": "", "manual_title": "",
    "output_chars": 900,
}


def _hold(page: Page, pattern: str) -> tuple[list, list]:
    """Hold every POST to ``pattern``; returns (requests seen, routes waiting)."""
    seen: list = []
    waiting: list[Route] = []

    def _handler(route: Route) -> None:
        if route.request.method != "POST":
            route.fallback()
            return
        seen.append(route.request.url)
        waiting.append(route)

    page.route(re.compile(pattern), _handler)
    return seen, waiting


def _answer(route: Route, body: dict, status: int = 200) -> None:
    route.fulfill(status=status, content_type="application/json", body=_json.dumps(body))


def test_stop_toasts_at_once_drops_a_second_tap_and_reports_the_outcome(
    authed_page: Page, base_url: str
) -> None:
    authed_page.route(
        re.compile(r".*/api/claude-code/sessions$"),
        lambda route: _answer(route, {"sessions": [_WORKER]}),
    )
    stops, waiting = _hold(authed_page, r".*/api/claude-code/sessions/[^/]+/stop$")
    stub_session_mirror(authed_page)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    open_session_row(authed_page, authed_page.locator('#sessionsList li[data-session-id="s-work"]'))

    def _menu_stop() -> None:
        authed_page.locator("#terminalMenu").click()
        authed_page.locator(".terminal-menu [data-item=action-stop-close]").click()

    _menu_stop()
    toast = authed_page.locator("#toast")
    expect(toast).to_contain_text("Stopping life-os…", timeout=_TOAST_MS)
    wait_until(authed_page, lambda: len(stops) == 1, "the first stop POST")

    # The first request is still unanswered: a second tap sends nothing.
    _menu_stop()
    flush_requests(authed_page)
    assert len(stops) == 1, stops
    expect(authed_page.locator("#terminalOverlay")).to_be_visible()

    # A refusal says why and never reads as a stop; the target is then free.
    _answer(waiting[0], {"detail": "session host busy"}, status=503)
    expect(toast).to_contain_text("Stop failed: session host busy")
    expect(toast).not_to_contain_text("Stop sent")
    expect(authed_page.locator("#terminalOverlay")).to_be_visible()

    _menu_stop()
    wait_until(authed_page, lambda: len(stops) == 2, "the retried stop POST")
    _answer(waiting[1], {"ok": True})
    expect(toast).to_contain_text("Stop sent to life-os")
    expect(authed_page.locator("#terminalOverlay")).to_be_hidden()


def test_life_os_resume_toasts_in_the_sheet_drops_a_second_tap_and_reports_done(
    authed_page: Page, base_url: str
) -> None:
    _mock_skills(authed_page)
    _mock_conversations(authed_page)
    launches, waiting = _hold(
        authed_page, r".*/api/life-os/skills/journal-daily/conversations/launch$")
    _open_conversations(authed_page, base_url)
    # Detached, so the resume lands in a console and no terminal overlay opens.
    life_launch_switch(authed_page, "#lifeOsDetached")
    _open_sheet(authed_page, 0)

    resume = authed_page.locator("#lifeOsConvoSheetResume")
    resume.click()
    # The page's toast sits under the modal sheet: the sheet's own carries it.
    expect(authed_page.locator("#lifeOsConvoToast")).to_contain_text("Resuming", timeout=_TOAST_MS)
    wait_until(authed_page, lambda: len(launches) == 1, "the resume POST")

    resume.click()
    flush_requests(authed_page)
    assert len(launches) == 1, launches
    expect(authed_page.locator("#lifeOsConvoSheet")).to_be_visible()

    _answer(waiting[0], {
        "launched": "journal-daily", "name": "journal-daily", "agent": "claude",
        "mode": "remote", "model": "claude-opus-5-5", "model_kept": True,
        "session": {"session_id": "x", "kind": "remote"},
    })
    expect(authed_page.locator("#lifeOsConvoSheet")).to_be_hidden()
    expect(authed_page.locator("#toast")).to_contain_text("Resumed")
    # Closed on success: the sheet's pending toast is not left for the next open.
    expect(authed_page.locator("#lifeOsConvoToast")).to_be_hidden()
