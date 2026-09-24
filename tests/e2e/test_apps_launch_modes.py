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


def test_rows_hide_path_and_url_and_launch_both_modes(
    authed_page: Page, base_url: str
) -> None:
    """Every Edit-mode-off Apps/Trays row check on one page load (#1215).

    Read-only steps first, then the launches; the captured launch list is
    asserted cumulatively, so each step's exact contribution is still pinned.
    """
    launches = _navigate(authed_page, base_url)

    # -- was test_path_is_hidden_until_edit_mode --
    # The bat path wraps to two or three lines on a phone and is only
    # wanted when renaming/removing, so #790 moved it behind Edit mode.
    row = authed_page.locator("#appsList li.action-row").first
    expect(row).to_be_visible(timeout=5_000)
    # One title line, one context line (#1128): the kind in sentence case
    # (#1156) folded into the context line, no path.
    expect(row.locator(".action-row-title")).to_have_text("Photo OCR")
    expect(row.locator(".action-row-meta")).to_have_text("Streamlit")

    # -- was test_tunnel_url_lives_in_the_row_menu_not_as_text --
    # #790: a cloudflared URL with a `?token=…` wrapped to three lines on
    # the phone and was only ever tapped. Since #1128 it is Open link and Copy
    # URL in the row's ⋯ menu, and never rendered as body text.
    tunnel = authed_page.locator('#appsList li.action-row[data-id="vt-tunnel"]')
    expect(tunnel).to_be_visible(timeout=5_000)
    assert "whisper.example.com" not in (tunnel.inner_text() or "")

    tunnel.locator(".action-row-kebab").click()
    expect(tunnel.locator(".app-tunnel-link")).to_be_enabled()
    expect(tunnel.locator(".app-copy-url-btn")).to_be_enabled()
    authed_page.keyboard.press("Escape")

    # A tunnel that isn't up offers the same rows, disabled; its context
    # line says it is down.
    dead = authed_page.locator('#appsList li.action-row[data-id="dead-tunnel"]')
    dead.locator(".action-row-kebab").click()
    expect(dead.locator(".app-tunnel-link")).to_be_disabled()
    expect(dead.locator(".app-copy-url-btn")).to_be_disabled()
    authed_page.keyboard.press("Escape")

    # -- was test_row_launches_visible_and_menu_launches_hidden --
    # #1128: tapping the row is the primary action (the visible-window
    # launch, #790's ⚡); Launch hidden (🚫👁) is in the ⋯ menu.
    expect(row).to_be_visible(timeout=5_000)

    # Visible: no `stealth` key at all, so the server keeps its default.
    row.locator(".action-row-main").click()
    expect(authed_page.locator("#toast")).to_contain_text("Launched Photo OCR")
    assert launches == [{}], f"visible launch sent {launches}"

    # Hidden: explicit opt-in to the windowless spawn.
    row.locator(".action-row-kebab").click()
    row.locator(".app-stealth-btn").click()
    expect(authed_page.locator("#toast")).to_contain_text("(stealth)")
    assert launches == [{}, {"stealth": True}], f"stealth launch sent {launches}"

    # -- was test_tray_rows_launch_the_same_way --
    # #790 applies to both bat-launching panels, not just Registered apps.
    tray = authed_page.locator("#registeredTraysList li.action-row").first
    expect(tray).to_be_visible(timeout=5_000)

    tray.locator(".action-row-kebab").click()
    tray.locator(".app-stealth-btn").click()
    # Cumulative: the tray's stealth launch is the one entry this step adds.
    assert launches == [{}, {"stealth": True}, {"stealth": True}], (
        f"tray stealth launch sent {launches[2:]}"
    )

    # The autostart switch is the row's one leading toggle and carries no
    # visible label — its accessible name is the only thing that must still
    # say what it does.
    toggle = tray.locator(":scope > button.toggle")
    expect(toggle).to_have_count(1)
    expect(toggle).to_have_attribute("aria-label", "Autostart Home Automation at boot")


def test_path_returns_in_edit_mode(authed_page: Page, base_url: str) -> None:
    _navigate(authed_page, base_url, edit=True)
    row = authed_page.locator("#appsList li.action-row").first
    expect(row).to_be_visible(timeout=5_000)
    expect(row.locator(".action-row-meta")).to_have_text("C:\\stub\\photo-ocr\\run.bat")
