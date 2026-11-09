"""Apps/Trays per-row launch modes — ⚡ visible vs 🚫👁 stealth (issue #790).

Before #790 the whole row was one launch button and every launch opened a
visible CMD window. Now the row body is inert and each row carries an
explicit pair: ⚡ launches as before, 🚫👁 posts ``stealth: true`` so
``spawn_bat`` swaps ``CREATE_NEW_CONSOLE`` for ``CREATE_NO_WINDOW``.

This file pins the *client* half of that contract — that the body no longer
launches, and that each button posts its own mode. The server half (the
creation flag actually chosen, and the older-client default) is pinned at
the unit/API layer in tests/unit/launching/test_launcher_spawn_bat.py and
tests/api/webapp/test_webapp_api_apps.py. Route-mocked: a real bat spawn is
far too heavy for the smoke suite. The Trays panel's tray-only rows and its
autostart switch ride on the same page load.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.ui import open_row_menu
from tests.e2e.fixtures.waits import flush_requests, wait_until

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
    # One title line and nothing else (#1505): the avatar says the kind, so
    # an idle app row has no context line, and never the path.
    expect(row.locator(".action-row-title")).to_have_text("Photo OCR")
    expect(row.locator(".action-row-meta")).to_have_count(0)

    # -- was test_tunnel_url_lives_in_the_row_menu_not_as_text --
    # #790: a cloudflared URL with a `?token=…` wrapped to three lines on
    # the phone and was only ever tapped. Since #1128 it is Open link and Copy
    # URL in the row's ⋯ menu, and never rendered as body text.
    tunnel = authed_page.locator('#appsList li.action-row[data-id="vt-tunnel"]')
    expect(tunnel).to_be_visible(timeout=5_000)
    assert "whisper.example.com" not in (tunnel.inner_text() or "")

    tunnel.locator(".action-row-kebab").click()
    menu = open_row_menu(authed_page)
    expect(menu.locator("[data-item=app-tunnel-link]")).to_be_enabled()
    expect(menu.locator("[data-item=app-copy-url-btn]")).to_be_enabled()
    authed_page.keyboard.press("Escape")

    # A tunnel that isn't up offers the same rows, disabled; its context
    # line says it is down.
    dead = authed_page.locator('#appsList li.action-row[data-id="dead-tunnel"]')
    dead.locator(".action-row-kebab").click()
    expect(menu.locator("[data-item=app-tunnel-link]")).to_be_disabled()
    expect(menu.locator("[data-item=app-copy-url-btn]")).to_be_disabled()
    authed_page.keyboard.press("Escape")

    # -- was test_row_launches_visible_and_menu_launches_hidden --
    # #1269: tapping the row launches hidden, the windowless spawn (#790's
    # 🚫👁) the unattended PC wants; Launch visible is in the ⋯ menu.
    expect(row).to_be_visible(timeout=5_000)
    expect(row.locator(".action-row-main")).to_have_attribute(
        "aria-label", "Launch Photo OCR hidden, with no window")

    # Hidden: the row tap opts in to the windowless spawn.
    row.locator(".action-row-main").click()
    expect(authed_page.locator("#toast")).to_contain_text("(stealth)")
    assert launches == [{"stealth": True}], f"row launch sent {launches}"

    # Visible: no `stealth` key at all, so the server keeps its default.
    row.locator(".action-row-kebab").click()
    expect(menu.locator("[data-item=app-visible-btn]")).to_have_text("Launch visible")
    # #1278: its icon is the eye (the pair to the row's eye-off), and the
    # <use> must resolve to a symbol in the page's inline sprite — a glyph
    # the per-app trim lacks renders as nothing.
    use = menu.locator("[data-item=app-visible-btn] svg use")
    expect(use).to_have_attribute("href", "#i-eye")
    assert use.evaluate(
        "u => !!document.querySelector('symbol' + u.getAttribute('href'))"
    ), "Launch visible's icon names a symbol the sprite does not have"
    expect(row.locator(".app-stealth-btn")).to_have_count(0)
    menu.locator("[data-item=app-visible-btn]").click()
    expect(authed_page.locator("#toast")).not_to_contain_text("(stealth)")
    assert launches == [{"stealth": True}, {}], f"visible launch sent {launches}"

    # -- was test_tray_rows_launch_the_same_way --
    # #790 applies to both bat-launching panels, not just Registered apps:
    # a tray row shares the renderer, so its tap launches hidden too.
    tray = authed_page.locator("#registeredTraysList li.action-row").first
    expect(tray).to_be_visible(timeout=5_000)

    tray.locator(".action-row-main").click()
    # Cumulative: the tray's hidden launch is the one entry this step adds.
    assert launches == [{"stealth": True}, {}, {"stealth": True}], (
        f"tray row launch sent {launches[2:]}"
    )

    # The autostart switch is the row's one leading toggle and carries no
    # visible label — its accessible name is the only thing that must still
    # say what it does.
    toggle = tray.locator(":scope > button.toggle")
    expect(toggle).to_have_count(1)
    expect(toggle).to_have_attribute("aria-label", "Autostart Home Automation at boot")

    _assert_trays_panel_and_autostart(authed_page, toggle)


def _assert_trays_panel_and_autostart(page: Page, toggle) -> None:
    """Only tray rows land in Trays, and the autostart switch PATCHes.

    Was test_registered_trays_panel.py's
    test_autostart_toggle_reflects_state_and_patches; last, since it PATCHes.
    """
    tray_rows = page.locator("#registeredTraysList li.action-row")
    expect(tray_rows).to_have_count(1)
    expect(tray_rows.first).to_contain_text("Home Automation")
    expect(page.locator("#appsList")).not_to_contain_text("Home Automation")
    expect(page.locator("#registeredTraysList")).not_to_contain_text("Photo OCR")

    # The registry answers with the flag the last PATCH set, as the server does.
    patches: list[dict] = []
    state = {"autostart": False}

    def _apps_handler(route):
        payload = _apps_payload()
        payload["apps"][-1]["autostart"] = state["autostart"]
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    def _patch_handler(route):
        body = json.loads(route.request.post_data or "{}")
        patches.append(body)
        state["autostart"] = bool(body.get("autostart"))
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"app": {"id": "home-automation-tray", "autostart": state["autostart"]}}),
        )

    page.route("**/api/apps", _apps_handler)
    page.route("**/api/apps/home-automation-tray", _patch_handler)

    # #790 dropped the switch's visible label — the panel is called Trays
    # and it is the row's only toggle; since #1128 it is the action-row's
    # one leading toggle.
    expect(toggle).to_have_class("toggle")
    expect(toggle.locator(".knob")).to_have_count(1)
    expect(toggle).to_have_attribute("aria-checked", "false")

    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "true")
    assert patches == [{"autostart": True}]

    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "false")
    assert patches == [{"autostart": True}, {"autostart": False}]


@pytest.mark.parametrize("edit", [False, True], ids=["edit-off", "edit-on"])
def test_path_and_rename_remove_live_in_the_menu(
    authed_page: Page, base_url: str, edit: bool
) -> None:
    """#1437 (decision 5 of #1432): Rename and Remove are in every app's
    kebab whatever the Jobs tab's Edit mode says. Since #1505 the menu's
    header names the kind, and the bat path is its Copy path item."""
    deletes: list = []

    def _delete(route):
        if route.request.method != "DELETE":
            route.fallback()
            return
        deletes.append(route.request.url)
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"removed": "photo-ocr-app"}))

    authed_page.route(re.compile(r".*/api/apps/photo-ocr-app$"), _delete)
    native: list = []
    authed_page.on("dialog", lambda d: (native.append(d.message), d.dismiss()))
    _navigate(authed_page, base_url, edit=edit)
    row = authed_page.locator("#appsList li.action-row").first
    expect(row).to_be_visible(timeout=5_000)
    expect(row.locator(".action-row-meta")).to_have_count(0)
    row.locator(".action-row-kebab").click()
    menu = open_row_menu(authed_page)
    expect(menu.locator(".row-menu-head")).to_have_text("Streamlit app")
    expect(menu.locator("[data-item=app-rename-btn]")).to_be_visible()
    # Copy path puts the bat path on the clipboard, never on screen.
    authed_page.evaluate(
        "() => { window.__copied = []; navigator.clipboard.writeText ="
        " t => { window.__copied.push(t); return Promise.resolve(); }; }")
    menu.locator("[data-item=app-copy-path-btn]").click()
    expect(authed_page.locator("#toast")).to_contain_text("Path copied")
    assert authed_page.evaluate("() => window.__copied") == ["C:\\stub\\photo-ocr\\run.bat"]
    row.locator(".action-row-kebab").click()
    # Remove is the danger item, last.
    expect(menu.locator(":scope > *").last).to_have_attribute("data-item", "app-remove-btn")

    # -- was test_apps_tab_glance.py's test_remove_asks_through_the_vendored_dialog --
    # Remove asks through the vendored confirm sheet: Escape is a "no", OK
    # sends the one DELETE, and no native dialog fires.
    menu.locator("[data-item=app-remove-btn]").click()
    confirm = authed_page.locator("#confirmDialog")
    expect(confirm).to_be_visible()
    expect(confirm.locator("#confirmDialogTitle")).to_have_text("Remove Photo OCR?")
    authed_page.keyboard.press("Escape")
    expect(confirm).to_be_hidden()
    flush_requests(authed_page)
    assert deletes == [], f"a dismissed confirm still removed: {deletes!r}"

    row.locator(".action-row-kebab").click()
    open_row_menu(authed_page).locator("[data-item=app-remove-btn]").click()
    confirm.locator("#confirmDialogOk").click()
    wait_until(authed_page, lambda: len(deletes) == 1, "the Remove DELETE")
    assert native == [], f"a native dialog fired: {native!r}"
