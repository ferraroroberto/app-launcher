"""Regression pin for #241 — an in-page loopback terminal is NOT the mirror.

The bug: ``terminal.js`` decided "this is the launcher-spawned PC mirror
window" purely from the connection origin (``state.status.terminal.reason
=== 'loopback'``). But a human's own desktop Chrome opened at
``https://127.0.0.1:8445`` is *also* loopback, so it was mis-classified as
the mirror — and on Stop & Close the cooperative ``{"type":"shutdown"}``
frame routed to ``close-mirror`` and ``window.close()``'d the user's actual
Chrome window.

The fix: a page is the mirror only when it was opened via the
``?terminal=<sid>`` deep-link (``state.isMirrorWindow``) *and* is loopback —
the deep-link is what the launcher's Edge ``--app`` window always carries and
a human navigating the SPA never does.

This test opens a session the way a desktop browser does — **in-app**, via
the running-sessions list, with no deep-link — over the loopback e2e harness,
and asserts it is treated as a normal (non-mirror) terminal: the ✏️ compose
button is visible and the unique mirror ``document.title`` is never set. It is
the mirror image of ``test_compose_bar.py::test_compose_button_hidden_in_mirror``
(deep-link open → mirror → compose hidden) and complements
``test_terminal_module_probes.py``'s routeFrame probe
(``routeFrame(_, isMirror=false)`` → ``swallow``,
never ``close-mirror``).
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import OVERLAY_OPEN_MS

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]


def test_inpage_loopback_open_is_not_treated_as_mirror(
    authed_page: Page, base_url: str, launched_pty_session: str, browser_name: str
) -> None:
    # Opening a session by tapping its row is an *in-page* terminal only on a
    # touch (phone) client now — a desktop browser (pointer: fine) opens a
    # dedicated PC Edge mirror window instead (issue #282). The conftest maps
    # the WebKit projection onto an iPhone (coarse pointer), so this in-page
    # path is exercised there; on the Chromium desktop projection the row-tap
    # mirrors and there is no in-page terminal to misclassify. The terminal.js
    # isMirror logic this pins is projection-independent, so WebKit covers it.
    if browser_name != "webkit":
        pytest.skip(
            "in-page terminal via row-tap is phone-only since #282; the "
            "desktop row-tap opens a mirror window (see "
            "test_desktop_session_mirror.py)"
        )
    sid = launched_pty_session

    # Land on the SPA with NO ?terminal deep-link — exactly how a desktop
    # browser sits on the launcher. boot() then sets state.isMirrorWindow
    # false, so even though the e2e harness connects from loopback the page
    # must not enter mirror mode.
    #
    # CRITICAL precondition: isMirror reads state.status.terminal.reason, and
    # over loopback that is 'loopback' — the exact signal the old code keyed
    # off. The terminal must be opened *after* /api/status has resolved, or
    # both old and new code see a null status and compute isMirror=false,
    # making this test pass vacuously (it would no longer fail on the bug).
    # boot() fetches its panels concurrently (#1258), so no other panel's
    # arrival proves the status is in: wait on /api/status itself.
    with authed_page.expect_response(
        lambda r: r.url.endswith("/api/status"), timeout=10_000
    ) as status:
        authed_page.goto(base_url, wait_until="domcontentloaded")
    status.value.finished()

    # Open the live terminal the in-app way: tap the running session's row
    # (sessions.js wires .session-open → openTerminal), not a deep-link.
    pty_row = authed_page.locator(
        "#sessionsList li.session-item:has(.session-kind.pty)"
    ).first
    expect(pty_row).to_be_visible(timeout=8_000)
    pty_row.locator(".session-open").click()

    authed_page.wait_for_selector(
        "#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS
    )

    # Non-mirror contract: the docked composer is shown (it is hidden only
    # in the PC mirror window), proving isMirror resolved false over loopback.
    expect(authed_page.locator("#terminalComposeBar")).to_be_visible()

    # And the unique mirror marker title — set only in mirror mode so the
    # launcher's EnumWindows can find the Edge --app window — is never applied
    # to the user's own browser tab.
    assert authed_page.title() != f"app-launcher-mirror-{sid}", (
        "an in-page loopback terminal must not take the mirror window title — "
        "it was mis-classified as the PC mirror (issue #241)"
    )
