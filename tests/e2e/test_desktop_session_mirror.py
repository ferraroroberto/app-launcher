"""Regression pin for #282 — desktop row-tap opens a mirror window, not in-page.

On a desktop browser (``pointer: fine``) tapping an existing session row must
open the same dedicated PC Edge ``--app`` mirror window a *new*-session launch
opens — so it can be closed without fear while the session keeps running —
rather than rendering the terminal inside the user's own browser (which is the
phone's behaviour). The row-tap therefore POSTs
``/api/claude-code/sessions/<sid>/mirror`` and the in-page ``#terminalOverlay``
must stay hidden.

The disposable autoboot harness is identified by ``LAUNCHER_SESSION_HOST_PORT``
(set only by the e2e/verify gate), so the route reports ``mirrored: true``
*without* spawning a real Edge window or touching the desktop — same isolation
rule the orphan-mirror sweep follows (issue #278). That makes this assertion
deterministic and side-effect-free.

The phone/WebKit counterpart (row-tap → in-page terminal) is pinned by
``test_inpage_terminal_not_mirror.py`` and ``test_stop_unify_and_terminal_kill.py``.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke


def test_desktop_row_tap_mirrors_and_keeps_inpage_terminal_closed(
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

    authed_page.goto(base_url, wait_until="domcontentloaded")
    expect(authed_page.locator("#buildReadout")).to_contain_text(
        "Build:", timeout=10_000
    )

    pty_row = authed_page.locator(
        f'#sessionsList li.session-item[data-session-id="{sid}"]'
    )
    expect(pty_row).to_be_visible(timeout=8_000)

    # The tap fires the mirror request; capture its response to prove the
    # desktop path routed to /mirror (not the in-page openTerminal).
    with authed_page.expect_response(
        lambda r: r.request.method == "POST"
        and f"/api/claude-code/sessions/{sid}/mirror" in r.url
    ) as resp_info:
        pty_row.locator(".session-open").click()

    resp = resp_info.value
    assert resp.ok, f"mirror POST failed: HTTP {resp.status}"
    assert resp.json().get("mirrored") is True, (
        "desktop row-tap must mirror to a PC window (#282)"
    )

    # The in-page terminal overlay must NOT open on desktop — the whole point is
    # a separate, freely-closable window (#282).
    expect(authed_page.locator("#terminalOverlay")).to_be_hidden()
