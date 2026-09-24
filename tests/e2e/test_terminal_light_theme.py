"""Terminal always follows the app theme (issue #383; supersedes #359).

Contract under test:
  * The terminal screen tokens follow ``html[data-theme]`` directly:
    light app theme → light ``--term-bg``/``--term-fg``, dark app theme →
    the dark screen. No opt-in switch exists.
  * An already-open terminal restyles live on a theme flip: terminal.js's
    MutationObserver pushes a fresh ``options.theme`` into xterm (CSS
    alone cannot recolor the renderer), driving the ``.xterm-viewport``
    background.
  * The machine-local terminal-themes.json (issue #381) still deep-merges
    over the built-in palette for the active mode.

The first bullet (the tokens flip, and no #359 opt-in switch remaining) needs
only a loaded page and runs in
``test_terminal_module_probes.py::test_terminal_module_contracts`` since
#1215; the tests here are the ones that need a live PTY.
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page

from tests.e2e.conftest import OVERLAY_OPEN_MS

pytestmark = pytest.mark.smoke


def test_open_terminal_restyles_live_on_theme_flip(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """Toggling theme while a terminal is open must recolor xterm live —
    no reopen. xterm mirrors options.theme.background onto its viewport
    element, so that is the observable.

    Also pins the JS half of the mirror-window close (issue #20): the
    ``app-launcher-mirror-<sid>`` marker trailing ``document.title``."""
    sid = launched_pty_session
    authed_page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")
    authed_page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)

    # Mirror-window close marker (issue #20, #266), formerly
    # test_edge_mirror_close.py::test_mirror_page_keeps_close_marker_in_document_title
    # — folded in by #1215, same ?terminal= deep link on the same stub PTY.
    # Loopback access auto-enters mirror mode: /api/status returns
    # {reachable: true, reason: 'loopback'}, which terminal.js picks up to
    # flip isMirror = true. The marker must remain at the tail of the title
    # (a human name may lead, issue #266) so the launcher's substring
    # EnumWindows scan still finds and closes the Edge --app window.
    marker = f"app-launcher-mirror-{sid}"
    authed_page.wait_for_function(
        f"() => document.title.endsWith({marker!r})",
        timeout=5_000,
    )

    authed_page.wait_for_selector(".xterm-viewport", timeout=OVERLAY_OPEN_MS)

    authed_page.evaluate("document.documentElement.dataset.theme = 'light'")
    authed_page.wait_for_function(
        "() => getComputedStyle(document.querySelector('.xterm-viewport'))"
        ".backgroundColor === 'rgb(255, 255, 255)'",
        timeout=5_000,
    )
    authed_page.evaluate("document.documentElement.dataset.theme = 'dark'")
    authed_page.wait_for_function(
        "() => getComputedStyle(document.querySelector('.xterm-viewport'))"
        ".backgroundColor === 'rgb(10, 10, 10)'",
        timeout=5_000,
    )


def test_user_theme_file_overrides_builtins(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """Issue #381: /api/terminal-themes (the machine-local
    terminal-themes.json) deep-merges over the built-in palette for the
    active mode — background here — and the overlay chrome follows."""
    sid = launched_pty_session

    # Serve a user theme before the SPA boots (wireTerminal fetches once).
    authed_page.route(
        re.compile(r".*/api/terminal-themes$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(
                {"themes": {"light": {"background": "#123456"}}}
            ),
        ),
    )

    authed_page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")
    authed_page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)
    authed_page.wait_for_selector(".xterm-viewport", timeout=OVERLAY_OPEN_MS)

    # Light theme → the user background (not the built-in white).
    authed_page.evaluate("document.documentElement.dataset.theme = 'light'")
    authed_page.wait_for_function(
        "() => getComputedStyle(document.querySelector('.xterm-viewport'))"
        ".backgroundColor === 'rgb(18, 52, 86)'",
        timeout=5_000,
    )
    # The overlay chrome follows the user background too.
    authed_page.wait_for_function(
        "() => document.getElementById('terminalOverlay')"
        ".style.background !== ''",
        timeout=5_000,
    )
    # Dark mode has no user override → the built-in dark screen.
    authed_page.evaluate("document.documentElement.dataset.theme = 'dark'")
    authed_page.wait_for_function(
        "() => getComputedStyle(document.querySelector('.xterm-viewport'))"
        ".backgroundColor === 'rgb(10, 10, 10)'",
        timeout=5_000,
    )
