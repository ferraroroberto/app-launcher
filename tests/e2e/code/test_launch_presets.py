"""Launch presets: a named model + effort picked beside the model dropdown.

One flow: Settings › Launch defaults › Launch presets adds, renames and removes
presets, the Coding toolbar then offers the one left beside "Default", and a
launch posts the preset's model and effort. Picking a model by hand afterwards
drops the effort, so it never outlives the model it was chosen for; the pick is
remembered on the device; with no preset nothing is drawn.

Hermetic: ``/api/config`` is passed through to the real disposable webapp with
its ``launch_presets`` swapped for an in-memory list that a POST edits, and the
launch endpoint is stubbed, so nothing is saved and no process spawns.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.ui import close_settings_sheets, open_settings_sheet

pytestmark = pytest.mark.smoke


def _mock(page: Page) -> list:
    """Serve an in-memory preset list from /api/config; a POST carrying one edits it."""
    store: list = []

    def config(route):
        request = route.request
        if request.method == "POST":
            body = request.post_data_json or {}
            if "launch_presets" in body:
                store[:] = [dict(p, id=p.get("id") or f"n{i}") for i, p in enumerate(body["launch_presets"])]
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))
                return
            route.continue_()
            return
        response = route.fetch()
        payload = response.json()
        payload["launch_presets"] = list(store)
        route.fulfill(response=response, body=json.dumps(payload))

    page.route("**/api/config", config)
    page.route("**/api/agents", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"agents": [{"id": "claude", "label": "Claude Code", "available": True}]})))
    page.route("**/api/apps", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"scan_root": "X", "apps": [
            {"id": "demo", "name": "demo", "kind": "claude-code", "project_dir": "X", "repo_url": None}]})))
    page.route("**/api/apps/*/launch", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"launched": "demo", "name": "demo", "kind": "claude-code",
                         "agent": "claude", "mode": "pty", "session": None})))
    return store


def _open_coding(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("details.projects-card").evaluate("el => { el.open = true; }")
    page.wait_for_selector(
        '#claudeList .coding-item[data-id="demo"] button.action-row-main[data-agent="claude"]',
        timeout=5_000,
    )


def _launch(page: Page) -> dict:
    with page.expect_request("**/api/apps/*/launch") as info:
        page.locator(
            '#claudeList .coding-item[data-id="demo"] button.action-row-main[data-agent="claude"]'
        ).click()
    return info.value.post_data_json


def _pick(page: Page, combo: str, value: str) -> None:
    """Open the ``combo`` dropdown ('codingPreset' or 'codingModel') and tap an option."""
    page.locator(f"#{combo}Btn").click()
    page.locator(f'#{combo}Menu [role="option"][data-value="{value}"]').click()


def _add_preset(page: Page, name: str, model: str, effort: str) -> None:
    page.locator("#launchPresetName").fill(name)
    page.locator("#launchPresetModel").select_option(model)
    page.locator("#launchPresetEffort").select_option(effort)
    page.locator("#launchPresetAdd").click()


def test_presets_are_managed_in_settings_and_launch_with_their_effort(
    authed_page: Page, base_url: str
) -> None:
    store = _mock(authed_page)
    _open_coding(authed_page, base_url)
    expect(authed_page.locator("#codingModelBtn")).to_be_visible()
    expect(authed_page.locator("#codingPresetCombo")).to_have_count(0)

    # Settings: add two, rename one in place, remove the other from its menu.
    open_settings_sheet(authed_page, "launchDefaultsSheet")
    _add_preset(authed_page, "Opus draft", "claude:opus", "xhigh")
    rows = authed_page.locator("#launchPresetList li")
    expect(rows).to_have_count(1)
    expect(rows.first.locator(".launch-preset-meta")).to_contain_text("Opus")
    _add_preset(authed_page, "Sonnet quick", "claude:sonnet", "low")
    expect(rows).to_have_count(2)
    name = rows.first.locator(".launch-preset-name")
    name.fill("Opus deep")
    name.press("Tab")
    expect(rows.first.locator(".launch-preset-name")).to_have_value("Opus deep")
    rows.nth(1).locator(".action-row-kebab").click()
    authed_page.locator('[data-item="launch-preset-remove-item"]').click()
    expect(rows).to_have_count(1)
    assert [(p["name"], p["model"], p["effort"]) for p in store] == [("Opus deep", "opus", "xhigh")]
    close_settings_sheets(authed_page)
    preset_id = store[0]["id"]

    _open_coding(authed_page, base_url)
    trigger = authed_page.locator("#codingPresetBtn")
    expect(trigger).to_have_text("Default")
    assert "effort" not in _launch(authed_page)

    _pick(authed_page, "codingPreset", preset_id)
    expect(trigger).to_have_text("Opus deep")
    expect(authed_page.locator("#codingModelBtn")).to_contain_text("Opus")
    payload = _launch(authed_page)
    assert (payload["model"], payload["effort"]) == ("claude:opus", "xhigh")

    # A model picked by hand turns the preset off: its effort never rides on.
    _pick(authed_page, "codingModel", "claude:sonnet")
    expect(trigger).to_have_text("Default")
    payload = _launch(authed_page)
    assert payload["model"] == "claude:sonnet" and "effort" not in payload

    _pick(authed_page, "codingPreset", preset_id)
    _pick(authed_page, "codingPreset", "default")
    expect(trigger).to_have_text("Default")
    assert "effort" not in _launch(authed_page)

    # The pick outlives a reload on this device.
    _pick(authed_page, "codingPreset", preset_id)
    authed_page.reload(wait_until="domcontentloaded")
    authed_page.locator("details.projects-card").evaluate("el => { el.open = true; }")
    expect(authed_page.locator("#codingPresetBtn")).to_have_text("Opus deep")
    assert _launch(authed_page)["effort"] == "xhigh"
