"""Apps/Trays per-row launch modes — ⚡ visible vs 🚫👁 stealth (issue #790).

Before #790 the whole row was one launch button and every launch opened a
visible CMD window. Now the row body is inert and each row carries an
explicit pair: ⚡ launches as before, 🚫👁 posts ``stealth: true`` so
``spawn_bat`` swaps ``CREATE_NEW_CONSOLE`` for ``CREATE_NO_WINDOW``.

This file pins the *client* half of that contract — that the body no longer
launches, and that each button posts its own mode. The server half (the
creation flag actually chosen, and the older-client default) is pinned at
the unit/API layer in tests/test_launcher_spawn_bat.py and
tests/test_webapp_api_apps.py. Route-mocked in the same style as
test_registered_trays_panel.py: a real bat spawn is far too heavy for the
smoke suite.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke


def _apps_payload() -> dict:
    return {
        "scan_root": "C:\\stub",
        "apps": [
            {
                "id": "photo-ocr-app",
                "name": "Photo OCR",
                "kind": "streamlit",
                "bat_path": "C:\\stub\\photo-ocr\\run.bat",
                "added_at": "2026-01-01T00:00:00",
                "autostart": False,
            },
            {
                "id": "vt-tunnel",
                "name": "Voice Transcriber",
                "kind": "tunnel",
                "bat_path": "C:\\stub\\voice-transcriber\\webapp_tunnel.bat",
                "tunnel_url": "https://whisper.example.com/?token=abc123",
                "health": "up",
                "added_at": "2026-01-01T00:00:00",
                "autostart": False,
            },
            {
                "id": "dead-tunnel",
                "name": "Photo OCR Tunnel",
                "kind": "tunnel",
                "bat_path": "C:\\stub\\photo-ocr\\webapp_tunnel.bat",
                "added_at": "2026-01-01T00:00:00",
                "autostart": False,
            },
            {
                "id": "home-automation-tray",
                "name": "Home Automation",
                "kind": "tray",
                "bat_path": "C:\\stub\\home-automation\\tray.bat",
                "added_at": "2026-01-01T00:00:00",
                "autostart": False,
            },
        ],
    }


def _navigate(page: Page, base_url: str, edit: bool = False) -> list[dict]:
    """Open the Apps tab with both panels expanded; return captured launches.

    Each entry is the decoded POST body of an ``/api/apps/{id}/launch``
    call — ``{}`` for a bodyless post, which is what an older cached PWA
    bundle sends and what the visible ⚡ launch sends too.

    ``edit`` seeds the Edit-mode flag before boot; ``state.editMode`` is
    read from localStorage at module init, so it has to be set before the
    page's scripts run rather than toggled afterwards.
    """
    page.add_init_script(
        "localStorage.setItem('launcher.editMode', '%s')" % ("1" if edit else "0")
    )
    launches: list[dict] = []

    def _launch_handler(route):
        raw = route.request.post_data or ""
        launches.append(json.loads(raw) if raw else {})
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"launched": "stub", "name": "stub", "kind": "streamlit"}),
        )

    page.route(
        "**/api/apps",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_apps_payload()),
        ),
    )
    page.route("**/api/apps/*/launch", _launch_handler)
    # Running-apps is polled right after a launch; stub it so the poll
    # can't race the assertions with a real (empty) round trip.
    page.route(
        "**/api/apps/running",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"running": []}),
        ),
    )

    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabApps").click()
    for card in (".apps-list-card", ".registered-trays-card"):
        if page.locator(card).get_attribute("open") is None:
            page.locator(f"{card} summary").click()
    return launches


def test_path_is_hidden_until_edit_mode(authed_page: Page, base_url: str) -> None:
    """The bat path wraps to two or three lines on a phone and is only
    wanted when renaming/removing, so #790 moved it behind Edit mode."""
    _navigate(authed_page, base_url)
    row = authed_page.locator("#appsList li.app-item").first
    expect(row).to_be_visible(timeout=5_000)
    expect(row.locator(".meta")).to_have_count(0)
    # The kind pill and the name are on their own lines, not sharing one.
    expect(row.locator(".app-row-kind .kind-pill")).to_have_text("streamlit")
    expect(row.locator(".app-row-name")).to_have_text("Photo OCR")


def test_path_returns_in_edit_mode(authed_page: Page, base_url: str) -> None:
    _navigate(authed_page, base_url, edit=True)
    row = authed_page.locator("#appsList li.app-item").first
    expect(row).to_be_visible(timeout=5_000)
    expect(row.locator(".meta")).to_have_text("C:\\stub\\photo-ocr\\run.bat")


def test_tunnel_url_is_a_link_icon_not_wrapped_text(
    authed_page: Page, base_url: str
) -> None:
    """#790: a cloudflared URL with a `?token=…` wrapped to three lines on
    the phone and was only ever tapped, so it became a 🔗 in the action
    cluster. The href must still carry the whole URL — the point is to hide
    the text, not to lose copy-link / open-in-new-tab."""
    _navigate(authed_page, base_url)
    row = authed_page.locator('#appsList li.app-item[data-id="vt-tunnel"]')
    expect(row).to_be_visible(timeout=5_000)

    link = row.locator("a.app-tunnel-link")
    expect(link).to_have_attribute("href", "https://whisper.example.com/?token=abc123")
    expect(link).to_have_attribute("target", "_blank")
    # The URL is reachable but never rendered as body text.
    assert "whisper.example.com" not in (row.inner_text() or "")

    # A tunnel that isn't up shows the same glyph, disabled — the row's
    # health dot is what reports up/down.
    dead = authed_page.locator('#appsList li.app-item[data-id="dead-tunnel"]')
    expect(dead.locator("a.app-tunnel-link")).to_have_count(0)
    expect(dead.locator(".app-launch-btn:disabled")).to_have_count(1)


def test_row_body_does_not_launch(authed_page: Page, base_url: str) -> None:
    """The name/path block is inert — #790 moved launching to the buttons."""
    launches = _navigate(authed_page, base_url)
    row = authed_page.locator("#appsList li.app-item").first
    expect(row).to_be_visible(timeout=5_000)

    body = row.locator(".launch-btn")
    expect(body).to_have_class("launch-btn inert")
    body.click()
    # No auto-retry here on purpose: assert the *absence* of a request.
    # A launch would have been captured synchronously by the route handler
    # long before the following visible-launch assertion runs.
    assert launches == [], f"row body still launched: {launches}"


def test_visible_and_stealth_buttons_post_their_own_mode(
    authed_page: Page, base_url: str
) -> None:
    launches = _navigate(authed_page, base_url)
    row = authed_page.locator("#appsList li.app-item").first
    expect(row.locator(".app-launch-btn")).to_have_count(2, timeout=5_000)

    # ⚡ — no `stealth` key at all, so the server keeps its visible default.
    row.locator('.app-launch-btn[data-stealth="0"]').click()
    expect(authed_page.locator("#toast")).to_contain_text("Launched Photo OCR")
    assert launches == [{}], f"visible launch sent {launches}"

    # 🚫👁 — explicit opt-in to the windowless spawn.
    row.locator('.app-launch-btn[data-stealth="1"]').click()
    expect(authed_page.locator("#toast")).to_contain_text("(stealth)")
    assert launches == [{}, {"stealth": True}], f"stealth launch sent {launches}"


def test_tray_rows_get_the_same_pair(authed_page: Page, base_url: str) -> None:
    """#790 applies to both bat-launching panels, not just Registered apps."""
    launches = _navigate(authed_page, base_url)
    row = authed_page.locator("#registeredTraysList li.app-item").first
    expect(row).to_be_visible(timeout=5_000)
    expect(row.locator(".launch-btn")).to_have_class("launch-btn inert")

    expect(row.locator(".app-launch-btn")).to_have_count(2)
    row.locator('.app-launch-btn[data-stealth="1"]').click()
    assert launches == [{"stealth": True}], f"tray stealth launch sent {launches}"

    # The autostart switch shares the launch line rather than owning a
    # full-width strip of its own, and carries no visible label — its
    # accessible name is the only thing that must still say what it does.
    toggle = row.locator(".app-launch-actions button.toggle")
    expect(toggle).to_have_count(1)
    expect(toggle).to_have_attribute("aria-label", "Autostart Home Automation at boot")
