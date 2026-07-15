"""Apps-tab "Registered Trays" panel (issue #456 part 2/2).

Mirrors test_running_apps.py's route-mocking style — the real registry +
tray-scanning path is covered at the unit/API layer
(test_scanner_apps.py's TestClassifyBat::test_tray*,
test_webapp_api_apps.py's TestPatchAppAutostart); this file exercises only
the SPA rendering + toggle wiring against a mocked /api/apps response.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke


def _apps_payload(autostart: bool = False) -> dict:
    return {
        "scan_root": "C:\\stub",
        "apps": [
            {
                "id": "home-automation-tray",
                "name": "Home Automation",
                "kind": "tray",
                "bat_path": "C:\\stub\\home-automation\\tray.bat",
                "added_at": "2026-01-01T00:00:00",
                "autostart": autostart,
            },
            {
                "id": "photo-ocr-app",
                "name": "Photo OCR",
                "kind": "streamlit",
                "bat_path": "C:\\stub\\photo-ocr\\run.bat",
                "added_at": "2026-01-01T00:00:00",
                "autostart": False,
            },
        ],
    }


def _navigate(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabApps").click()
    # The panel is a collapsed <details> by default — expand it.
    summary = page.locator(".registered-trays-card summary")
    if page.locator(".registered-trays-card").get_attribute("open") is None:
        summary.click()


def test_only_tray_kind_rows_appear_in_registered_trays_panel(
    authed_page: Page, base_url: str
) -> None:
    authed_page.route(
        "**/api/apps",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_apps_payload()),
        ),
    )
    _navigate(authed_page, base_url)

    tray_rows = authed_page.locator("#registeredTraysList li.app-item")
    expect(tray_rows).to_have_count(1, timeout=5_000)
    expect(tray_rows.first).to_contain_text("Home Automation")

    # The streamlit row must land in Registered apps, not here.
    other_rows = authed_page.locator("#appsList li.app-item")
    expect(other_rows).to_have_count(1)
    expect(other_rows.first).to_contain_text("Photo OCR")


def test_empty_state_shown_with_no_tray_rows(
    authed_page: Page, base_url: str
) -> None:
    payload = _apps_payload()
    payload["apps"] = [payload["apps"][1]]  # streamlit row only
    authed_page.route(
        "**/api/apps",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        ),
    )
    _navigate(authed_page, base_url)
    expect(authed_page.locator("#registeredTraysEmpty")).to_be_visible()


def test_autostart_toggle_reflects_state_and_patches(
    authed_page: Page, base_url: str
) -> None:
    patches: list[dict] = []
    state = {"autostart": False}

    def _apps_handler(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_apps_payload(autostart=state["autostart"])),
        )

    def _patch_handler(route):
        body = json.loads(route.request.post_data or "{}")
        patches.append(body)
        state["autostart"] = bool(body.get("autostart"))
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"app": {"id": "home-automation-tray", "autostart": state["autostart"]}}),
        )

    authed_page.route("**/api/apps", _apps_handler)
    authed_page.route("**/api/apps/home-automation-tray", _patch_handler)
    _navigate(authed_page, base_url)

    toggle = authed_page.locator(
        "#registeredTraysList li.app-item .tray-autostart-row button"
    )
    expect(toggle).to_have_class("toggle")
    expect(toggle.locator(".knob")).to_have_count(1)
    expect(toggle).to_have_attribute("aria-checked", "false")

    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "true")
    assert patches == [{"autostart": True}]

    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "false")
    assert patches == [{"autostart": True}, {"autostart": False}]
