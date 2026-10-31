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


def _press_mic_with(page: Page, base_url: str, sid: str, session: dict) -> None:
    page.route(
        "**/api/desktop-session",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(session)
        ),
    )
    _open_composer(page, base_url, sid)
    page.evaluate(_NO_DEVICE)
    page.locator("#terminalComposeBar .composer-mic").click()


def test_a_remote_session_on_this_pc_shows_the_remote_desktop_hint(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    _press_mic_with(
        authed_page, base_url, launched_pty_session, {"state": "remote", "this_pc": True}
    )
    toast = authed_page.locator("#toast")
    expect(toast).to_contain_text("No microphone in this remote-desktop session")
    expect(toast).to_contain_text("Record from this computer")


@pytest.mark.parametrize(
    "session",
    [
        {"state": "local", "this_pc": True},
        {"state": "unknown", "this_pc": True},
        {"state": "remote", "this_pc": False},
    ],
    ids=["local", "unknown", "remote-but-another-device"],
)
def test_every_other_answer_keeps_the_generic_message(
    authed_page: Page, base_url: str, launched_pty_session: str, session: dict
) -> None:
    _press_mic_with(authed_page, base_url, launched_pty_session, session)
    toast = authed_page.locator("#toast")
    expect(toast).to_contain_text("Microphone unavailable")
    expect(toast).not_to_contain_text("remote-desktop")


@pytest.mark.parametrize(
    "session, shown",
    [
        ({"state": "remote", "this_pc": True}, True),
        ({"state": "local", "this_pc": True}, False),
        ({"state": "unknown", "this_pc": True}, False),
        ({"state": "remote", "this_pc": False}, False),
    ],
    ids=["remote", "local", "unknown", "remote-but-another-device"],
)
def test_settings_voice_line_shows_only_for_a_remote_session_on_this_pc(
    authed_page: Page, base_url: str, session: dict, shown: bool
) -> None:
    authed_page.route(
        "**/api/desktop-session",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(session)
        ),
    )
    authed_page.goto(base_url, wait_until="domcontentloaded")
    open_settings(authed_page)
    parts = authed_page.locator("#paneSettings [data-voice-remote]")
    if shown:
        expect(parts.locator("visible=true")).to_have_count(3)
        expect(authed_page.locator("#voiceRemoteHelp")).to_contain_text("Record from this computer")
    else:
        expect(parts.locator("visible=true")).to_have_count(0)
