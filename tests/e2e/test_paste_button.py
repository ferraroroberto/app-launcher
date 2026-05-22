"""Regression pin for issue #29 (📋 paste button silent failure in iOS PWA).

The bug: tapping ``#terminalPaste`` did nothing in iOS Safari PWA — the
JS reached ``navigator.clipboard.readText()`` but no input arrived at
the session-host. The handler is at ``terminal.js:468-479``.

Approach: mock ``navigator.clipboard.readText`` via init script (Playwright
WebKit headless clipboard perms are not reliable), seed a payload that
exercises the #13 regression class (curly braces + newline), click the
button, then assert the bytes arrived in the per-session log. The check
runs in both projections — the iOS bug only repros on WebKit but
Chromium's pass confirms the click handler itself didn't regress.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.smoke

_PASTE_PAYLOAD = "p4s7e-{regress}\n"

# Override navigator.clipboard before the SPA loads. defineProperty is
# the most portable path — direct assignment fails in WebKit because
# navigator.clipboard is non-writable in some contexts.
_CLIPBOARD_MOCK = """
((payload) => {
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: {
      readText: async () => payload,
      writeText: async () => {},
    },
  });
})(%r)
""" % _PASTE_PAYLOAD


def test_paste_button_forwards_clipboard_to_pty(
    authed_page: Page,
    base_url: str,
    launched_pty_session: str,
    e2e_ui_timeout: int,
    wait_for_session_log,
) -> None:
    sid = launched_pty_session
    authed_page.add_init_script(_CLIPBOARD_MOCK)
    authed_page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")

    # Wait for the terminal overlay to mount and the WS to reach OPEN —
    # the paste handler is a no-op before then (terminal.js:470). The
    # status line gets `hidden = true` once setTerminalStatus(null) fires
    # in ws.onopen (terminal.js:64); wait_for_function avoids the
    # wait_for_selector default "visible" check, which would never
    # resolve against a hidden-attribute element.
    authed_page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=e2e_ui_timeout)
    authed_page.wait_for_function(
        "() => document.getElementById('terminalStatus') "
        "&& document.getElementById('terminalStatus').hidden === true",
        timeout=e2e_ui_timeout,
    )

    authed_page.locator("#terminalPaste").click()

    # session_input writes one [input] line per chunk; repr() escapes the
    # newline so we search for the literal escaped form, not the raw byte.
    assert wait_for_session_log(authed_page, sid, "p4s7e-{regress}"), (
        f"paste button click did not deliver clipboard text to "
        f"webapp/sessions/{sid}.log within the input-delivery budget — the "
        "clipboard -> ConPTY -> log round-trip timed out (raise "
        "LAUNCHER_E2E_LOG_DEADLINE_MS for a slow runner)"
    )
