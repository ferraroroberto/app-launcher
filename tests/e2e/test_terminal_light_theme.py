"""Opt-in light terminal theme (issue #359).

Contract under test:
  * Default is unchanged — the terminal screen tokens stay dark in BOTH
    app themes (the pre-#359 theme-invariant behavior).
  * The "Terminal follows app theme" switch in Settings persists to
    localStorage and stamps ``html[data-term-theme="follow"]``, which
    flips ``--term-bg``/``--term-fg`` light — but only while the app
    theme is light; dark theme keeps the same dark screen either way.
  * An already-open terminal restyles live on a theme flip: terminal.js's
    MutationObserver pushes a fresh ``options.theme`` into xterm (CSS
    alone cannot recolor the renderer), driving the ``.xterm-viewport``
    background.
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.smoke


def _term_bg(page: Page) -> str:
    return page.evaluate(
        "getComputedStyle(document.documentElement)"
        ".getPropertyValue('--term-bg').trim()"
    )


def test_default_terminal_stays_dark_in_both_themes(
    authed_page: Page, base_url: str
) -> None:
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.evaluate("document.documentElement.dataset.theme = 'light'")
    assert _term_bg(authed_page) == "#0a0a0a"
    authed_page.evaluate("document.documentElement.dataset.theme = 'dark'")
    assert _term_bg(authed_page) == "#0a0a0a"


def test_follow_app_switch_flips_tokens_and_persists(
    authed_page: Page, base_url: str
) -> None:
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.evaluate("document.getElementById('settingsPanel').open = true")
    authed_page.locator("#termFollowTheme").click()
    assert (
        authed_page.locator("#termFollowTheme").get_attribute("aria-checked")
        == "true"
    )

    # Light app theme + opt-in → light screen tokens.
    authed_page.evaluate("document.documentElement.dataset.theme = 'light'")
    assert _term_bg(authed_page) == "#ffffff"
    # Dark app theme keeps the dark screen even with the opt-in.
    authed_page.evaluate("document.documentElement.dataset.theme = 'dark'")
    assert _term_bg(authed_page) == "#0a0a0a"

    # Persisted like the theme itself: survives a reload.
    authed_page.reload(wait_until="domcontentloaded")
    assert (
        authed_page.locator("#termFollowTheme").get_attribute("aria-checked")
        == "true"
    )
    authed_page.evaluate("document.documentElement.dataset.theme = 'light'")
    assert _term_bg(authed_page) == "#ffffff"

    # Switch off restores the invariant dark screen (and un-stamps the attr).
    authed_page.evaluate("document.getElementById('settingsPanel').open = true")
    authed_page.locator("#termFollowTheme").click()
    assert _term_bg(authed_page) == "#0a0a0a"


def test_open_terminal_restyles_live_on_theme_flip(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """Toggling theme while a terminal is open must recolor xterm live —
    no reopen. xterm mirrors options.theme.background onto its viewport
    element, so that is the observable."""
    sid = launched_pty_session
    authed_page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")
    authed_page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=10_000)
    authed_page.wait_for_selector(".xterm-viewport", timeout=10_000)

    # Opt in (the switch is behind the overlay — drive it directly).
    authed_page.evaluate("document.getElementById('termFollowTheme').click()")

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
    authed_page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=10_000)
    authed_page.wait_for_selector(".xterm-viewport", timeout=10_000)

    # Opt in + light theme → the user background (not the built-in white).
    authed_page.evaluate("document.getElementById('termFollowTheme').click()")
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
