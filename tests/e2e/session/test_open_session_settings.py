"""Where a session opens: the two Settings switches, the row tap and the kebab (#1955).

``open_sessions_in_window`` (off by default) decides whether a desktop browser
sends a session's terminal to the PC Edge ``--app`` window; ``open_sessions_in_chat``
(on by default) makes Chat the view a never-opened session opens in, on the
phone too. The row menu's "Open in new window" reaches the window either way,
on a desktop browser only.

The disposable harness reports ``mirrored: true`` from the mirror route without
spawning a window (``LAUNCHER_SESSION_HOST_PORT``), so every assertion here is
about the request the page sends. ``GET /api/config`` is overlaid so a box's own
settings never decide the test, and the switches' POSTs are answered without
reaching disk.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.routes import fulfill_edited_json
from tests.e2e.fixtures.ui import open_row_menu, open_settings
from tests.e2e.fixtures.waits import OVERLAY_OPEN_MS, wait_until

pytestmark = pytest.mark.smoke

_CONFIG = re.compile(r".*/api/config$")


def _mock_config(page: Page, *, window: bool, chat: bool) -> dict:
    """Overlay the two switches on GET /api/config and record the POSTs."""
    knobs = {"window": window, "chat": chat, "posts": []}

    def _route(route):
        if route.request.method == "POST":
            body = route.request.post_data_json or {}
            knobs["posts"].append(body)
            knobs["window"] = body.get("open_sessions_in_window", knobs["window"])
            knobs["chat"] = body.get("open_sessions_in_chat", knobs["chat"])
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": True}))
            return
        def _overlay(body: dict) -> None:
            body["open_sessions_in_window"] = knobs["window"]
            body["open_sessions_in_chat"] = knobs["chat"]

        fulfill_edited_json(route, _overlay)

    page.route(_CONFIG, _route)
    return knobs


def _track_mirror_posts(page: Page, sid: str) -> list:
    posts: list = []
    page.on(
        "request",
        lambda r: posts.append(r.url)
        if r.method == "POST" and f"/api/claude-code/sessions/{sid}/mirror" in r.url else None,
    )
    return posts


def _session_row(page: Page, base_url: str, sid: str):
    page.goto(base_url, wait_until="domcontentloaded")
    row = page.locator(f'#sessionsList li.session-item[data-session-id="{sid}"]')
    expect(row).to_be_visible(timeout=8_000)
    return row


@pytest.mark.iphone
def test_settings_switches_show_the_defaults_and_save_as_they_change(
    authed_page: Page, base_url: str
) -> None:
    knobs = _mock_config(authed_page, window=False, chat=True)
    authed_page.goto(base_url, wait_until="domcontentloaded")
    open_settings(authed_page)

    window = authed_page.locator("#openSessionsInWindowToggle")
    chat = authed_page.locator("#openSessionsInChatToggle")
    expect(window).to_have_attribute("aria-checked", "false")
    expect(chat).to_have_attribute("aria-checked", "true")

    window.click()
    wait_until(authed_page, lambda: {"open_sessions_in_window": True} in knobs["posts"],
               "the window switch's POST")
    expect(window).to_have_attribute("aria-checked", "true")
    chat.click()
    wait_until(authed_page, lambda: {"open_sessions_in_chat": False} in knobs["posts"],
               "the Chat switch's POST")
    expect(chat).to_have_attribute("aria-checked", "false")


def test_desktop_defaults_keep_the_terminal_in_the_page_and_the_menu_opens_the_window(
    authed_page: Page, base_url: str, launched_pty_session: str, browser_name: str
) -> None:
    if browser_name != "chromium":
        pytest.skip("desktop (pointer: fine) behaviour; the phone case is the next test")
    sid = launched_pty_session
    _mock_config(authed_page, window=False, chat=True)
    mirror_posts = _track_mirror_posts(authed_page, sid)
    row = _session_row(authed_page, base_url, sid)
    overlay = authed_page.locator("#terminalOverlay")

    # Nothing remembered: Chat, in the page.
    row.locator(".session-open").click()
    expect(overlay).to_be_visible()
    expect(authed_page.locator("#sessionModeChat")).to_have_attribute("aria-pressed", "true")

    # Terminal is remembered for the session. With the window switch off the
    # next tap renders the terminal in the page: no PC window.
    authed_page.locator("#sessionModeTerminal").click()
    expect(authed_page.locator("#sessionModeTerminal")).to_have_attribute("aria-pressed", "true")
    authed_page.locator("#terminalBack").click()
    expect(overlay).to_be_hidden()
    row.locator(".session-open").click()
    authed_page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)
    expect(authed_page.locator("#sessionModeTerminal")).to_have_attribute("aria-pressed", "true")
    assert mirror_posts == [], f"the switch is off, so a tap must not mirror: {mirror_posts}"
    authed_page.locator("#terminalBack").click()
    expect(overlay).to_be_hidden()

    # The menu item is the way to the window, and leaves the page alone.
    row.locator(".session-kebab").click()
    with authed_page.expect_response(
        lambda r: r.request.method == "POST"
        and f"/api/claude-code/sessions/{sid}/mirror" in r.url
    ) as resp_info:
        open_row_menu(authed_page).locator("[data-item=session-open-window-btn]").click()
    assert resp_info.value.ok
    assert resp_info.value.json().get("mirrored") is True
    expect(overlay).to_be_hidden()


def test_desktop_window_switch_on_mirrors_a_terminal_tap(
    authed_page: Page, base_url: str, launched_pty_session: str, browser_name: str
) -> None:
    if browser_name != "chromium":
        pytest.skip("desktop (pointer: fine) behaviour")
    sid = launched_pty_session
    _mock_config(authed_page, window=True, chat=False)
    mirror_posts = _track_mirror_posts(authed_page, sid)
    row = _session_row(authed_page, base_url, sid)

    # Chat off: the first tap resolves to Terminal, which the switch sends to the window.
    with authed_page.expect_response(
        lambda r: r.request.method == "POST"
        and f"/api/claude-code/sessions/{sid}/mirror" in r.url
    ):
        row.locator(".session-open").click()
    assert len(mirror_posts) == 1
    expect(authed_page.locator("#terminalOverlay")).to_be_hidden()


@pytest.mark.iphone
def test_phone_opens_chat_first_and_its_menu_has_no_window_item(
    authed_page: Page, base_url: str, launched_pty_session: str, browser_name: str
) -> None:
    if browser_name != "webkit":
        pytest.skip("phone (pointer: coarse) behaviour; the desktop cases are above")
    sid = launched_pty_session
    _mock_config(authed_page, window=False, chat=True)
    mirror_posts = _track_mirror_posts(authed_page, sid)
    row = _session_row(authed_page, base_url, sid)

    row.locator(".session-kebab").click()
    menu = open_row_menu(authed_page)
    expect(menu.locator("[data-item=session-terminal-btn]")).to_be_visible()
    expect(menu.locator("[data-item=session-open-window-btn]")).to_have_count(0)
    authed_page.keyboard.press("Escape")

    row.locator(".session-open").click()
    authed_page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)
    expect(authed_page.locator("#sessionModeChat")).to_have_attribute("aria-pressed", "true")
    assert mirror_posts == []
