"""Regression pin for #253 — unified stop button + kill from terminal view.

Two iPhone bites in one issue:

1. Each running-sessions row has exactly **one** 🛑 Stop-and-kill button
   (the old ⏹ "leave window open" + ⏏ "stop & close" pair collapsed to one).
2. The in-page terminal view has a 🛑 Kill button beside the ‹ back arrow,
   so a finished session can be stopped without going back to the list
   first. Killing it returns to the list (the overlay hides).

Complements ``test_smoke.py``'s per-row button assertion; this one drives
the terminal-view kill end to end.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke


def test_terminal_view_has_back_arrow_and_kill_button(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    authed_page.goto(base_url, wait_until="domcontentloaded")
    expect(authed_page.locator("#buildReadout")).to_contain_text(
        "Build:", timeout=10_000
    )

    # Target the row for the session THIS test launched, by id — never
    # ".first", which on a shared/live host could be the user's own session
    # (issue #260). The disposable autoboot host makes this deterministic.
    pty_row = authed_page.locator(
        f'#sessionsList li.session-item[data-session-id="{launched_pty_session}"]'
    )
    expect(pty_row).to_be_visible(timeout=8_000)

    # The row carries a single stop control — the unified 🛑 (issue #253).
    expect(pty_row.locator(".action-stop-close")).to_have_count(1)
    expect(pty_row.locator(".action-stop:not(.action-stop-close)")).to_have_count(0)

    pty_row.locator(".session-open").click()
    authed_page.wait_for_selector(
        "#terminalOverlay:not([hidden])", timeout=10_000
    )

    # Both the icon-only back arrow and the kill button live in the bar.
    expect(authed_page.locator("#terminalBack")).to_be_visible()
    expect(authed_page.locator("#terminalKill")).to_be_visible()


def test_kill_from_terminal_view_stops_and_returns_to_list(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    authed_page.goto(base_url, wait_until="domcontentloaded")
    expect(authed_page.locator("#buildReadout")).to_contain_text(
        "Build:", timeout=10_000
    )

    # Scope to the session this test launched (issue #260) — never ".first".
    pty_row = authed_page.locator(
        f'#sessionsList li.session-item[data-session-id="{launched_pty_session}"]'
    )
    expect(pty_row).to_be_visible(timeout=8_000)
    pty_row.locator(".session-open").click()
    authed_page.wait_for_selector(
        "#terminalOverlay:not([hidden])", timeout=10_000
    )

    # One tap stops — stopSession() no longer guards with a confirm() dialog
    # (issue #253 follow-up); a stray dialog would mean the guard came back.
    authed_page.on("dialog", lambda d: pytest.fail(f"unexpected dialog: {d.message}"))

    authed_page.locator("#terminalKill").click()

    # The stop POST waits out the graceful-then-force window on the host;
    # on success stopSession() hides the overlay (we were viewing the
    # session it stopped). to_be_hidden() (not wait_for_selector, which
    # waits for *visibility*) is what asserts the overlay closed. Generous
    # timeout to cover the force-fallback.
    expect(authed_page.locator("#terminalOverlay")).to_be_hidden(timeout=12_000)
