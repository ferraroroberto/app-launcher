"""Regression pin for issue #37 (mobile compose bar for predictive text).

The feature: a ``✏️`` toolbar button toggles a slim ``<textarea>`` compose
bar above the iOS keyboard. xterm.js wipes its helper textarea after
every keystroke, so iOS/Android predictive keyboards can't suggest there
— the compose bar is a normal textarea with default predictive
attributes. ``➤`` Send forwards ``<text> + \\r`` to the PTY over the
existing WS ``input`` frame.

The e2e harness connects from loopback, so every terminal open is
detected as the PC mirror (``isMirror`` true). That is itself the case
issue #37 verification step 4 pins: the ``✏️`` button must be hidden in
the mirror. To exercise the Send path we un-hide the toggle button and
drive the real handler — the Send logic is not mirror-gated, only the
button's visibility is.

Predictive suggestions themselves are an OS-keyboard behaviour and can
only be confirmed on a real phone; this test pins the wiring underneath.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SESSIONS_DIR = _REPO_ROOT / "webapp" / "sessions"


def _read_session_log(sid: str) -> str:
    log_path = _SESSIONS_DIR / f"{sid}.log"
    if not log_path.exists():
        return ""
    return log_path.read_text(encoding="utf-8", errors="replace")


def _open_terminal(page: Page, base_url: str, sid: str) -> None:
    page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")
    page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=10_000)
    page.wait_for_function(
        "() => document.getElementById('terminalStatus') "
        "&& document.getElementById('terminalStatus').hidden === true",
        timeout=10_000,
    )


def test_compose_button_hidden_in_mirror(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """Loopback open is the PC mirror — the ✏️ button must stay hidden."""
    _open_terminal(authed_page, base_url, launched_pty_session)
    expect(authed_page.locator("#terminalCompose")).to_be_hidden()
    expect(authed_page.locator("#terminalComposeBar")).to_be_hidden()


def test_compose_send_forwards_text_to_pty(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """➤ Send forwards the textarea contents + Enter to the PTY."""
    sid = launched_pty_session
    _open_terminal(authed_page, base_url, sid)

    # The button is hidden under loopback (mirror) — un-hide it so the
    # real toggle handler / setComposeOpen() runs. Send itself is not
    # mirror-gated, so this exercises the genuine production path.
    authed_page.evaluate(
        "document.getElementById('terminalCompose').hidden = false"
    )
    authed_page.locator("#terminalCompose").click()
    expect(authed_page.locator("#terminalComposeBar")).to_be_visible()

    payload = "compose-{regress}"
    authed_page.locator("#terminalComposeInput").fill(payload)
    authed_page.locator("#terminalComposeSend").click()

    # Bar clears and stays open after Send.
    expect(authed_page.locator("#terminalComposeInput")).to_have_value("")
    expect(authed_page.locator("#terminalComposeBar")).to_be_visible()

    deadline_ms = 5_000
    found = False
    for _ in range(deadline_ms // 200):
        if payload in _read_session_log(sid):
            found = True
            break
        authed_page.wait_for_timeout(200)
    assert found, (
        f"➤ Send did not deliver the compose text to the session log "
        f"({_SESSIONS_DIR / (sid + '.log')}) within 5s — issue #37 regressed"
    )
