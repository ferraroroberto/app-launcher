"""Regression pin for issue #36 (on-screen keys popover in mobile terminal).

The feature: the terminal bar's ``^C``/``⏹`` buttons were replaced by a
single ``⌨️`` button that toggles a D-pad popover. Each key sends the
matching VT/xterm escape sequence over the existing WS ``input`` channel
so iPhone keyboards without arrows/Esc/Tab can drive Claude's TUI prompts.

Approach mirrors ``test_paste_button.py``: open the terminal overlay,
wait for the WS to reach OPEN, toggle the popover, tap keys, then assert
the escape bytes arrived in the per-session log. ``session_input`` writes
each chunk through ``repr()``, so ``\\x1b[B`` lands in the log as the
literal escaped form. Runs in both projections.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke


def test_keys_popover_sends_escape_sequences(
    authed_page: Page,
    base_url: str,
    launched_pty_session: str,
    e2e_ui_timeout: int,
    wait_for_session_log,
) -> None:
    sid = launched_pty_session
    authed_page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")

    authed_page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=e2e_ui_timeout)
    authed_page.wait_for_function(
        "() => document.getElementById('terminalStatus') "
        "&& document.getElementById('terminalStatus').hidden === true",
        timeout=e2e_ui_timeout,
    )

    popover = authed_page.locator("#terminalKeysPopover")
    expect(popover).to_be_hidden()

    # Toggle the popover open, tap ↓ — it must stay open for chained nav.
    authed_page.locator("#terminalKeys").click()
    expect(popover).to_be_visible()
    authed_page.locator('#terminalKeysPopover .key-btn[data-key="down"]').click()
    expect(popover).to_be_visible()
    assert wait_for_session_log(authed_page, sid, "\\x1b[B"), (
        "↓ key did not deliver the down-arrow escape sequence to "
        f"webapp/sessions/{sid}.log within the input-delivery budget — the "
        "key -> ConPTY -> log round-trip timed out (raise "
        "LAUNCHER_E2E_LOG_DEADLINE_MS for a slow runner)"
    )

    # Enter sends \r and closes the popover (Enter usually ends a prompt).
    authed_page.locator('#terminalKeysPopover .key-btn[data-key="enter"]').click()
    expect(popover).to_be_hidden()


def test_no_stale_ctrlc_quit_buttons(authed_page: Page, base_url: str) -> None:
    """The ^C / Quit buttons are gone — only the new ⌨️ button remains."""
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    assert authed_page.locator("#terminalCtrlC").count() == 0
    assert authed_page.locator("#terminalQuit").count() == 0
    assert authed_page.locator("#terminalKeys").count() == 1
