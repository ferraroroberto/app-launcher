"""Desktop row-tap: Chat in-page by default, the mirror window for Terminal.

#282: on a desktop browser (``pointer: fine``) a session's *terminal* is the
dedicated PC Edge ``--app`` mirror window a new-session launch opens, so it
can be closed without fear while the session keeps running, never a terminal
inside the user's own browser. The row-tap then POSTs
``/api/claude-code/sessions/<sid>/mirror`` and ``#terminalOverlay`` stays
hidden.

#1273: a session with a transcript reader now opens in **Chat** by default
on the desktop, in-page, with no mirror request. Terminal is the explicit
choice, remembered per session (``launcher.sessionModes``); once a session
was last viewed in Terminal, its row-tap mirrors out as before.

The disposable autoboot harness is identified by ``LAUNCHER_SESSION_HOST_PORT``
(set only by the e2e/verify gate), so the route reports ``mirrored: true``
*without* spawning a real Edge window or touching the desktop — same isolation
rule the orphan-mirror sweep follows (issue #278). That makes this assertion
deterministic and side-effect-free.

The phone/WebKit counterpart (row-tap → in-page terminal, Terminal first) is
pinned by ``test_session_mode_toggle.py::test_row_tap_reopens_last_mode`` and
``test_inpage_terminal_not_mirror.py``.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke


def test_desktop_row_tap_opens_chat_then_mirrors_once_terminal_is_chosen(
    authed_page: Page, base_url: str, launched_pty_session: str, browser_name: str
) -> None:
    # Desktop (pointer: fine) behaviour. The conftest maps WebKit onto an
    # iPhone (coarse pointer), which opens the terminal in-page instead, so
    # this runs on the Chromium desktop projection only (#282).
    if browser_name != "chromium":
        pytest.skip(
            "desktop (pointer: fine) behaviour; the iPhone/WebKit projection "
            "opens the terminal in-page — see test_inpage_terminal_not_mirror.py"
        )
    sid = launched_pty_session
    mirror_posts: list = []
    authed_page.on(
        "request",
        lambda r: mirror_posts.append(r.url)
        if r.method == "POST" and f"/api/claude-code/sessions/{sid}/mirror" in r.url else None,
    )

    authed_page.goto(base_url, wait_until="domcontentloaded")
    pty_row = authed_page.locator(
        f'#sessionsList li.session-item[data-session-id="{sid}"]'
    )
    expect(pty_row).to_be_visible(timeout=8_000)

    # First open, nothing remembered: Chat, in-page, no mirror window (#1273).
    pty_row.locator(".session-open").click()
    overlay = authed_page.locator("#terminalOverlay")
    expect(overlay).to_be_visible()
    expect(authed_page.locator("#sessionModeChat")).to_have_attribute("aria-pressed", "true")
    assert mirror_posts == [], f"a Chat open must not mirror: {mirror_posts}"

    # Choosing Terminal is remembered for this session; back to the list.
    authed_page.locator("#sessionModeTerminal").click()
    expect(authed_page.locator("#sessionModeTerminal")).to_have_attribute("aria-pressed", "true")
    authed_page.locator("#terminalBack").click()
    expect(overlay).to_be_hidden()

    # The next tap resolves to Terminal, which on a desktop is the mirror
    # window (#282): the row POSTs /mirror and the in-page overlay stays shut.
    with authed_page.expect_response(
        lambda r: r.request.method == "POST"
        and f"/api/claude-code/sessions/{sid}/mirror" in r.url
    ) as resp_info:
        pty_row.locator(".session-open").click()

    resp = resp_info.value
    assert resp.ok, f"mirror POST failed: HTTP {resp.status}"
    assert resp.json().get("mirrored") is True, (
        "a desktop Terminal open must mirror to a PC window (#282)"
    )
    expect(overlay).to_be_hidden()
