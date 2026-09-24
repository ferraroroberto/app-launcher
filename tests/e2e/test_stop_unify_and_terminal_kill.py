"""Regression pin for #253 — unified stop button + kill from terminal view.

Two iPhone bites in one issue:

1. Each running-sessions row has exactly **one** 🛑 Stop-and-kill button
   (the old ⏹ "leave window open" + ⏏ "stop & close" pair collapsed to one).
2. The in-page terminal view can stop its session without going back to the
   list first. Killing it returns to the list (the overlay hides). Since
   #981 the kill lives in the bar's ⋮ session menu (Rename · Copy link ·
   Stop and kill) instead of a permanent ✕ beside the back arrow.

Complements ``test_smoke.py``'s per-row button assertion; this one drives
the terminal-view kill end to end.

Both tests open the terminal by tapping the session row, which is an *in-page*
terminal only on a touch (phone) client now — a desktop browser opens a
dedicated PC Edge mirror window instead (issue #282). So they run on the
iPhone (WebKit) projection, where the in-page terminal view they exercise is
the real behaviour; the desktop row-tap is covered by
``test_desktop_session_mirror.py``.
"""

from __future__ import annotations

import os

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import OVERLAY_OPEN_MS

pytestmark = pytest.mark.smoke

# How long to wait for the terminal overlay to hide after a kill (issue #286).
# stopSession() awaits the *entire* /stop POST, which on the host runs the
# graceful quit's grace window (``_STOP_GRACE_SECONDS`` = 5 s) then the
# force-fallback before responding; only then does the client hide the overlay.
# So the hide budget is network + up to 5 s grace + force + shutdown signal +
# network — a fixed 12 s could be exceeded on a loaded WebKit/iPhone projection,
# the same timing-flake class as the PTY-input-delivery tests (#58/#184). Env-
# tunable like ``E2E_LOG_POLL_DEADLINE_MS`` so the slow hosted runner gets
# headroom (e2e.yml widens it on CI) without slowing the local pass.
_STOP_OVERLAY_HIDE_MS = int(os.environ.get("E2E_STOP_OVERLAY_HIDE_MS", "20000"))


def _skip_unless_phone(browser_name: str) -> None:
    # conftest projects WebKit onto an iPhone (coarse pointer → in-page
    # terminal); the Chromium desktop projection mirrors the row-tap (#282).
    if browser_name != "webkit":
        pytest.skip(
            "in-page terminal view via row-tap is phone-only since #282; the "
            "desktop row-tap opens a mirror window"
        )


def test_terminal_view_has_back_arrow_and_session_menu(
    authed_page: Page, base_url: str, launched_pty_session: str, browser_name: str
) -> None:
    _skip_unless_phone(browser_name)
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

    # The row's stop is the one inside its kebab menu (#1025) — the unified
    # 🛑 (issue #253). It is never a control loose on the row itself, and
    # the menu stays closed until the kebab is tapped.
    expect(pty_row.locator(".session-menu .action-stop-close")).to_have_count(1)
    expect(pty_row.locator(".session-menu")).to_be_hidden()
    expect(pty_row.locator(".action-stop:not(.action-stop-close)")).to_have_count(0)
    expect(pty_row.locator(".row-actions > .action-stop-close")).to_have_count(0)

    pty_row.locator(".session-open").click()
    authed_page.wait_for_selector(
        "#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS
    )

    # The icon-only back arrow and the ⋮ session menu live in the bar; the
    # old permanent ✕ and ↓ are gone from it (#981).
    expect(authed_page.locator("#terminalBack")).to_be_visible()
    expect(authed_page.locator("#terminalMenu")).to_be_visible()
    expect(authed_page.locator("#terminalKill")).to_have_count(0)
    expect(authed_page.locator("#terminalJumpEnd")).to_have_count(0)

    # ⋮ opens Rename · Copy link · Compact (#1218, a Claude session) · Stop
    # and kill, in that order.
    authed_page.locator("#terminalMenu").click()
    menu = authed_page.locator("#terminalOverlay .terminal-menu")
    expect(menu).to_be_visible()
    expect(menu.locator(".row-menu-label")).to_have_text(
        ["Rename", "Copy link", "Compact", "Stop and kill"]
    )
    # The overlay's own copy of the unified stop (#253) — the same path the
    # row menu's Stop calls, reached from inside the session view.
    expect(menu.locator(".action-stop-close")).to_have_count(1)
    expect(authed_page.locator("#terminalMenu")).to_have_attribute(
        "aria-expanded", "true"
    )


def test_kill_from_terminal_view_stops_and_returns_to_list(
    authed_page: Page, base_url: str, launched_pty_session: str, browser_name: str
) -> None:
    _skip_unless_phone(browser_name)
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
        "#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS
    )

    # One tap stops — stopSession() no longer guards with a confirm() dialog
    # (issue #253 follow-up); a stray dialog would mean the guard came back.
    authed_page.on("dialog", lambda d: pytest.fail(f"unexpected dialog: {d.message}"))

    # Two taps from the terminal since #981, and since #1025 the only taps
    # there are: ⋮, then Stop and kill.
    authed_page.locator("#terminalMenu").click()
    authed_page.get_by_role("menuitem", name="Stop and kill session").click()

    # The stop POST waits out the graceful-then-force window on the host;
    # on success stopSession() hides the overlay (we were viewing the
    # session it stopped). to_be_hidden() (not wait_for_selector, which
    # waits for *visibility*) is what asserts the overlay closed. The budget
    # must exceed the host's worst-case stop window (grace + force) plus the
    # two network hops on a loaded runner — env-tunable for CI headroom (#286).
    expect(authed_page.locator("#terminalOverlay")).to_be_hidden(
        timeout=_STOP_OVERLAY_HIDE_MS
    )
