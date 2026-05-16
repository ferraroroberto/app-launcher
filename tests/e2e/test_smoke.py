"""Smoke tests for the launcher webapp (issue #22).

Tight by design: ~6 checks that catch the bugs we actually hit (JS
exceptions on boot, empty config form, broken tab switch, wrong stop
buttons per session kind, missing login overlay markup). Expand
iteratively in follow-up issues; do NOT turn this file into a regression
net for every feature.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke


def _navigate_collecting_errors(page: Page, base_url: str) -> list[str]:
    """Open the SPA and capture any uncaught JS errors during boot."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    # #sessionsList is rendered server-side in index.html; waiting for it
    # confirms the static document parsed without an early script crash.
    page.wait_for_selector("#sessionsList", state="attached", timeout=5_000)
    return errors


def test_page_loads_without_console_errors(authed_page: Page, base_url: str) -> None:
    errors = _navigate_collecting_errors(authed_page, base_url)
    # Give the boot script a beat to settle: fetchConfig, sessions poll,
    # listeners poll. Anything thrown during that fans out as pageerror.
    authed_page.wait_for_timeout(500)
    assert errors == [], f"JS errors during boot:\n  - " + "\n  - ".join(errors)


def test_claude_options_populated(authed_page: Page, base_url: str) -> None:
    _navigate_collecting_errors(authed_page, base_url)
    # renderClaudeOptions() runs after /api/config resolves; wait for the
    # first button under each segmented control to attach.
    authed_page.wait_for_selector("#claudeModel > button", timeout=5_000)
    authed_page.wait_for_selector("#claudeEffort > button", timeout=5_000)
    model_count = authed_page.locator("#claudeModel > button").count()
    effort_count = authed_page.locator("#claudeEffort > button").count()
    assert model_count >= 1, f"#claudeModel rendered no buttons (got {model_count})"
    assert effort_count >= 1, f"#claudeEffort rendered no buttons (got {effort_count})"


def test_sessions_panel_renders(authed_page: Page, base_url: str) -> None:
    _navigate_collecting_errors(authed_page, base_url)
    sessions_list = authed_page.locator("#sessionsList")
    expect(sessions_list).to_be_attached()
    # Either the list has rows OR the empty-state paragraph is visible —
    # both are valid; we only fail if neither is true.
    items = sessions_list.locator("li.session-item").count()
    if items == 0:
        empty = authed_page.locator("#sessionsEmpty")
        expect(empty).to_be_visible()
        expect(empty).to_have_text(
            "No sessions launched from here yet — tap a project below to start one."
        )


def test_tabs_switch(authed_page: Page, base_url: str) -> None:
    _navigate_collecting_errors(authed_page, base_url)
    pane_claude = authed_page.locator("#paneClaude")
    pane_apps = authed_page.locator("#paneApps")

    expect(pane_claude).to_be_visible()
    expect(pane_apps).to_be_hidden()

    authed_page.locator("#tabApps").click()
    expect(pane_apps).to_be_visible()
    expect(pane_claude).to_be_hidden()
    # Substring match on className so future class reorders don't false-fail.
    expect(authed_page.locator("#tabApps")).to_have_class(re.compile(r"\bactive\b"))

    authed_page.locator("#tabClaude").click()
    expect(pane_claude).to_be_visible()
    expect(pane_apps).to_be_hidden()


def test_pty_session_renders_with_both_stop_buttons(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """End-to-end: the launched PTY session shows up with both stop buttons.

    The `launched_pty_session` fixture POSTs /api/apps/.../launch (mode=pty)
    before the test and force-kills the session in teardown — so this test
    no longer depends on the user having something running.

    Other session rows (if any) are also checked: detached → only ⏏️,
    full-control → both ⏹ and ⏏️. The launched session must be among
    the full-control rows.
    """
    _navigate_collecting_errors(authed_page, base_url)
    # Poll for the freshly-launched session to surface. The SPA refreshes
    # the list on a 5 s timer; we'd rather not wait that long, so hit the
    # refresh button directly (it triggers an immediate fetchSessions).
    authed_page.locator("#refreshSessions").click()
    pty_rows = authed_page.locator("#sessionsList li.session-item:has(.session-kind.pty)")
    expect(pty_rows.first).to_be_visible(timeout=5_000)

    rows = authed_page.locator("#sessionsList li.session-item")
    count = rows.count()
    assert count >= 1, "fixture launched a session but #sessionsList is empty"

    saw_pty = False
    for i in range(count):
        row = rows.nth(i)
        kind = row.locator(".session-kind").inner_text().strip().lower()
        stop = row.locator(".action-stop")
        stop_close = row.locator(".action-stop-close")
        if "detached" in kind:
            assert stop.count() == 0, f"row {i} ({kind}): unexpected ⏹ Stop button"
            assert stop_close.count() == 1, f"row {i} ({kind}): missing ⏏ button"
        elif "full control" in kind:
            saw_pty = True
            assert stop.count() == 1, f"row {i} ({kind}): missing ⏹ Stop button"
            assert stop_close.count() == 1, f"row {i} ({kind}): missing ⏏ button"
            expect(stop.first).to_be_visible()
            expect(stop_close.first).to_be_visible()
        else:
            pytest.fail(f"row {i}: unrecognised session kind text {kind!r}")

    assert saw_pty, "launched PTY session did not surface as a 'full control' row"


def test_login_overlay_dom_present(authed_page: Page, base_url: str) -> None:
    """The login overlay markup is wired so showLogin() can reveal it.

    We exercise the DOM directly rather than triggering a real 401: the
    bearer middleware bypasses loopback (server.py:267), so a bad token
    from 127.0.0.1 won't surface the overlay. This still catches the
    regression we care about — overlay element + password input missing
    or renamed.
    """
    _navigate_collecting_errors(authed_page, base_url)
    overlay = authed_page.locator("#loginOverlay")
    expect(overlay).to_be_hidden()
    # Flip the hidden attr the same way showLogin() does.
    authed_page.evaluate(
        "document.getElementById('loginOverlay').hidden = false"
    )
    expect(overlay).to_be_visible()
    pw = authed_page.locator("#loginPassword")
    expect(pw).to_be_editable()
    pw.fill("dummy")
    expect(pw).to_have_value("dummy")
