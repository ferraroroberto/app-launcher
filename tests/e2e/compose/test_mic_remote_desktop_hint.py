"""The missing-mic toast names Remote Desktop only when that is the cause.

A Remote Desktop session has no mic unless the client redirects it, and the
browser then reports ``NotFoundError``. The launcher asks the server whether the
PC's session is a remote one (and whether the caller is a browser on this PC);
only ``remote`` from this PC shows the specific hint. Every other answer keeps
the generic "Microphone unavailable" toast. ``getUserMedia`` is stubbed to
reject and ``/api/desktop-session`` is mocked; no real microphone is touched.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.compose.test_mic_single_tap import _open_composer
from tests.e2e.fixtures.ui import open_settings

pytestmark = pytest.mark.smoke

_NO_DEVICE = """
() => {
  navigator.mediaDevices.getUserMedia = () => {
    const err = new Error('Requested device not found');
    err.name = 'NotFoundError';
    return Promise.reject(err);
  };
}
"""


# Every answer the server can give, with whether it earns the specific hint.
_ANSWERS = [
    ({"state": "remote", "this_pc": True}, True),
    ({"state": "local", "this_pc": True}, False),
    ({"state": "unknown", "this_pc": True}, False),
    ({"state": "remote", "this_pc": False}, False),
]


def _route_desktop_session(page: Page) -> dict:
    """Mock /api/desktop-session with whatever ``answer["body"]`` holds when
    the request lands: the client reads it live on every mic press and every
    Settings open, so one page can walk every answer."""
    answer: dict = {"body": {}}
    page.route(
        "**/api/desktop-session",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(answer["body"])
        ),
    )
    return answer


def test_only_a_remote_session_on_this_pc_turns_the_mic_toast_into_the_hint(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """``remote`` from this PC names Remote Desktop; every other answer keeps
    the generic "Microphone unavailable" toast. The toast is emptied before
    each press, so each check reads that press's own toast."""
    answer = _route_desktop_session(authed_page)
    _open_composer(authed_page, base_url, launched_pty_session)
    authed_page.evaluate(_NO_DEVICE)
    mic = authed_page.locator("#terminalComposeBar .composer-mic")
    toast = authed_page.locator("#toast")
    for session, hinted in _ANSWERS:
        answer["body"] = session
        authed_page.evaluate("() => { document.getElementById('toast').textContent = ''; }")
        mic.click()
        if hinted:
            expect(toast).to_contain_text("No microphone in this remote-desktop session")
            expect(toast).to_contain_text("Record from this computer")
        else:
            expect(toast).to_contain_text("Microphone unavailable")
            expect(toast).not_to_contain_text("remote-desktop")


def test_settings_voice_line_shows_only_for_a_remote_session_on_this_pc(
    authed_page: Page, base_url: str
) -> None:
    """Settings re-reads the answer on every open. Each answer that hides the
    line follows an open that showed it, so its check sees the line go away
    rather than a page that never showed it."""
    remote, *others = _ANSWERS
    answer = _route_desktop_session(authed_page)
    authed_page.goto(base_url, wait_until="domcontentloaded")
    parts = authed_page.locator("#paneSettings [data-voice-remote]")
    for session, _shown in others:
        answer["body"] = remote[0]
        open_settings(authed_page)
        expect(parts.locator("visible=true")).to_have_count(3)
        expect(authed_page.locator("#voiceRemoteHelp")).to_contain_text(
            "Record from this computer"
        )
        authed_page.locator("#tabClaude").click()

        answer["body"] = session
        open_settings(authed_page)
        expect(parts.locator("visible=true")).to_have_count(0)
        authed_page.locator("#tabClaude").click()
