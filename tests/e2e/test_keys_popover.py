"""Regression pin for issue #36 (on-screen keys popover in mobile terminal).

The feature: the terminal bar's ``^C``/``⏹`` buttons were replaced by a
single ``⌨️`` button that toggles a D-pad popover. Each key sends the
matching VT/xterm escape sequence over the existing WS ``input`` channel
so iPhone keyboards without arrows/Esc/Tab can drive Claude's TUI prompts.
Since #980 the button is the composer grid's second slot and the popover
floats above the composer (composer.js / terminal-keys.js). Since #986 the
popover also carries a sticky Ctrl toggle exposing C/U/X control bytes,
mutually exclusive with ⇧.

Approach: open the terminal overlay, wait for the WS to reach OPEN, un-hide
the composer (the loopback harness opens every terminal as the PC mirror,
where it is hidden by design — the handlers are not mirror-gated), toggle
the popover, tap keys, then assert the escape bytes arrived in the
per-session log. ``session_input`` writes
each chunk through ``repr()``, so ``\\x1b[B`` lands in the log as the
literal escaped form. Runs in both projections.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import OVERLAY_OPEN_MS

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]


def test_keys_popover_sends_escape_sequences(
    authed_page: Page,
    base_url: str,
    launched_pty_session: str,
    wait_for_session_log,
) -> None:
    sid = launched_pty_session
    authed_page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")

    authed_page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)
    authed_page.wait_for_function(
        "() => document.getElementById('terminalStatus') "
        "&& document.getElementById('terminalStatus').hidden === true",
        timeout=OVERLAY_OPEN_MS,
    )

    authed_page.evaluate("document.getElementById('terminalComposeBar').hidden = false")
    popover = authed_page.locator("#terminalComposeBar .keys-popover")
    expect(popover).to_be_hidden()

    # Toggle the popover open, tap ↓ — it must stay open for chained nav.
    authed_page.locator("#terminalComposeBar .composer-keys").click()
    expect(popover).to_be_visible()
    authed_page.locator('#terminalComposeBar .keys-popover .key-btn[data-key="down"]').click()
    expect(popover).to_be_visible()
    assert wait_for_session_log(authed_page, sid, "\\x1b[B"), (
        "↓ key did not deliver the down-arrow escape sequence to "
        f"webapp/sessions/{sid}.log — it never reached the live PTY session"
    )

    # Enter sends \r and closes the popover (Enter usually ends a prompt).
    authed_page.locator('#terminalComposeBar .keys-popover .key-btn[data-key="enter"]').click()
    expect(popover).to_be_hidden()


def test_esc_key_sends_bare_escape_and_closes(
    authed_page: Page,
    base_url: str,
    launched_pty_session: str,
    wait_for_session_log,
) -> None:
    """Issue #987: Esc delivers exactly one bare ``\\x1b`` and closes the popover.

    The phone leg of the Esc path. Every other key's bytes *start* with
    ``\\x1b``, so a substring match on it would pass for an arrow or a focus
    report; the needle is the whole logged chunk. The agent leg (a real
    ConPTY acting on that byte) is pinned by ``tests/test_claude_pty_esc.py``.
    """
    sid = launched_pty_session
    authed_page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")

    authed_page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)
    authed_page.wait_for_function(
        "() => document.getElementById('terminalStatus') "
        "&& document.getElementById('terminalStatus').hidden === true",
        timeout=OVERLAY_OPEN_MS,
    )

    authed_page.evaluate("document.getElementById('terminalComposeBar').hidden = false")
    popover = authed_page.locator("#terminalComposeBar .keys-popover")
    authed_page.locator("#terminalComposeBar .composer-keys").click()
    expect(popover).to_be_visible()

    authed_page.locator('#terminalComposeBar .keys-popover .key-btn[data-key="esc"]').click()
    expect(popover).to_be_hidden()
    assert wait_for_session_log(authed_page, sid, "[input] '\\x1b'\n"), (
        "Esc did not deliver a bare \\x1b chunk to "
        f"webapp/sessions/{sid}.log — the phone can't dismiss or interrupt the agent"
    )


def test_shift_toggle_sends_back_tab(
    authed_page: Page,
    base_url: str,
    launched_pty_session: str,
    wait_for_session_log,
) -> None:
    """Issue #137: ⇧ is a sticky toggle — ⇧ then Tab sends Shift+Tab (\\x1b[Z).

    The back-tab sequence is how Claude Code cycles permission modes. ⇧ sends
    nothing on its own and stays engaged across taps until tapped off or the
    popover closes.
    """
    sid = launched_pty_session
    authed_page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")

    authed_page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)
    authed_page.wait_for_function(
        "() => document.getElementById('terminalStatus') "
        "&& document.getElementById('terminalStatus').hidden === true",
        timeout=OVERLAY_OPEN_MS,
    )

    authed_page.evaluate("document.getElementById('terminalComposeBar').hidden = false")
    popover = authed_page.locator("#terminalComposeBar .keys-popover")
    shift = authed_page.locator('#terminalComposeBar .keys-popover .key-btn[data-key="shift"]')
    authed_page.locator("#terminalComposeBar .composer-keys").click()
    expect(popover).to_be_visible()

    # Engage Shift — it lights up and stays open; nothing is sent yet.
    shift.click()
    expect(shift).to_have_class(re.compile(r"\bactive\b"))
    expect(popover).to_be_visible()

    # Tab now delivers back-tab (Shift+Tab) and the popover stays open so the
    # cycle can be chained.
    authed_page.locator('#terminalComposeBar .keys-popover .key-btn[data-key="tab"]').click()
    expect(popover).to_be_visible()
    assert wait_for_session_log(authed_page, sid, "\\x1b[Z"), (
        "⇧ + Tab did not deliver the back-tab (Shift+Tab) escape sequence to "
        f"webapp/sessions/{sid}.log — mode-cycling from the phone is broken"
    )

    # Tapping ⇧ again releases the sticky modifier.
    shift.click()
    expect(shift).not_to_have_class(re.compile(r"\bactive\b"))


def test_ctrl_toggle_sends_control_c(
    authed_page: Page,
    base_url: str,
    launched_pty_session: str,
    wait_for_session_log,
) -> None:
    """Issue #986: Ctrl is a sticky toggle like ⇧ — Ctrl then C sends \\x03
    (interrupt). C/U/X are disabled until Ctrl is armed (so a stray tap can't
    type a letter into the prompt), and Ctrl/⇧ are mutually exclusive.
    """
    sid = launched_pty_session
    authed_page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")

    authed_page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)
    authed_page.wait_for_function(
        "() => document.getElementById('terminalStatus') "
        "&& document.getElementById('terminalStatus').hidden === true",
        timeout=OVERLAY_OPEN_MS,
    )

    authed_page.evaluate("document.getElementById('terminalComposeBar').hidden = false")
    popover = authed_page.locator("#terminalComposeBar .keys-popover")
    ctrl = authed_page.locator('#terminalComposeBar .keys-popover .key-btn[data-key="ctrl"]')
    shift = authed_page.locator('#terminalComposeBar .keys-popover .key-btn[data-key="shift"]')
    c_key = authed_page.locator('#terminalComposeBar .keys-popover .key-btn[data-key="c"]')
    authed_page.locator("#terminalComposeBar .composer-keys").click()
    expect(popover).to_be_visible()

    # C is disabled until Ctrl is armed.
    expect(c_key).to_be_disabled()

    # Engage Ctrl — it lights up, C becomes tappable, nothing is sent yet.
    ctrl.click()
    expect(ctrl).to_have_class(re.compile(r"\bactive\b"))
    expect(c_key).to_be_enabled()

    # Engaging ⇧ releases Ctrl (mutually exclusive) and re-disables C.
    shift.click()
    expect(shift).to_have_class(re.compile(r"\bactive\b"))
    expect(ctrl).not_to_have_class(re.compile(r"\bactive\b"))
    expect(c_key).to_be_disabled()
    shift.click()  # release ⇧ again before re-arming Ctrl

    # Re-arm Ctrl, tap C — the interrupt byte reaches the live PTY session,
    # and the popover stays open (Ctrl+C may need a second tap).
    ctrl.click()
    expect(ctrl).to_have_class(re.compile(r"\bactive\b"))
    c_key.click()
    expect(popover).to_be_visible()
    assert wait_for_session_log(authed_page, sid, "\\x03"), (
        "Ctrl + C did not deliver the interrupt byte to "
        f"webapp/sessions/{sid}.log — the phone can't interrupt a live agent"
    )

    # Ctrl stays armed across the tap (chaining a second interrupt); closing
    # the popover releases it.
    expect(ctrl).to_have_class(re.compile(r"\bactive\b"))
    ctrl.click()
    expect(ctrl).not_to_have_class(re.compile(r"\bactive\b"))
    expect(c_key).to_be_disabled()


def test_ctrl_c_delivers_exactly_one_interrupt_byte(
    authed_page: Page,
    base_url: str,
    launched_pty_session: str,
    wait_for_session_log,
    session_log_path,
) -> None:
    """Issue #1024: one C tap is one ``\\x03`` — the launcher never doubles it.

    #1024 reported that one Ctrl+C from the popover does not cancel a Grok
    turn, and proposed sending whatever Grok needs. Live ConPTY probes of
    Grok Build 1.0.34 disproved the premise: a single ``\\x03`` cancels a
    running turn ("Turn cancelled by user"), and the "press again to quit"
    the report saw is Grok's *idle* Ctrl+C — the quit confirmation it prints
    when there is no turn to cancel. So the fix is no byte change, and this
    pins that: the obvious wrong fix (``CTRL_KEY_BYTES.c = '\\x03\\x03'``, or
    a second ``send`` per tap) would be a second interrupt for Claude Code,
    which acts on the first.

    ``test_ctrl_toggle_sends_control_c`` above pins the toggle mechanics and
    that the byte arrives at all; this one counts. The needle is the whole
    logged chunk (``[input] '\\x03'\\n``) because ``session_input`` writes one
    ``repr()``-ed line per chunk, so a doubled byte logs as ``'\\x03\\x03'``
    and fails the equality rather than hiding inside a substring match.
    """
    sid = launched_pty_session
    authed_page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")

    authed_page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)
    authed_page.wait_for_function(
        "() => document.getElementById('terminalStatus') "
        "&& document.getElementById('terminalStatus').hidden === true",
        timeout=OVERLAY_OPEN_MS,
    )

    authed_page.evaluate("document.getElementById('terminalComposeBar').hidden = false")
    popover = authed_page.locator("#terminalComposeBar .keys-popover")
    authed_page.locator("#terminalComposeBar .composer-keys").click()
    expect(popover).to_be_visible()
    authed_page.locator('#terminalComposeBar .keys-popover .key-btn[data-key="ctrl"]').click()
    authed_page.locator('#terminalComposeBar .keys-popover .key-btn[data-key="c"]').click()

    needle = "[input] '\\x03'\n"
    assert wait_for_session_log(authed_page, sid, needle), (
        f"Ctrl + C did not deliver a bare \\x03 chunk to webapp/sessions/{sid}.log"
    )
    # Settle past the poller's own granularity so a (wrongly) queued second
    # write would have landed before the count is taken.
    authed_page.wait_for_timeout(500)
    log = session_log_path(sid).read_text(encoding="utf-8", errors="replace")
    interrupts = [ln for ln in log.splitlines() if ln.endswith("[input] '\\x03'")]
    assert len(interrupts) == 1, (
        "one C tap must send exactly one interrupt byte; the session log holds "
        f"{len(interrupts)} — Claude Code acts on the first \\x03, so a second "
        f"is a second interrupt. Input lines: {[ln for ln in log.splitlines() if '[input]' in ln]}"
    )


def test_no_stale_ctrlc_quit_buttons(authed_page: Page, base_url: str) -> None:
    """The ^C / Quit buttons are gone — only the ⌨️ button remains, and since
    #980 it lives in the composer grid, not the terminal bar."""
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    assert authed_page.locator("#terminalCtrlC").count() == 0
    assert authed_page.locator("#terminalQuit").count() == 0
    assert authed_page.locator("#terminalOverlay .terminal-bar .composer-keys").count() == 0
    assert authed_page.locator("#terminalComposeBar .composer-keys").count() == 1
