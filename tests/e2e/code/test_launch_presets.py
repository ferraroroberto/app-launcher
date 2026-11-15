"""Launch presets: a named model + effort picked beside the model dropdown.

Settings › Launch defaults › Launch presets adds, renames and removes them;
the Coding toolbar then offers each beside "Default", and a launch posts the
preset's model and effort. Picking a model by hand afterwards drops the
effort, so it never outlives the model it was chosen for.

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

OPUS_DEEP = {"id": "p1", "name": "Opus deep", "agent": "claude", "model": "opus", "effort": "xhigh"}
SONNET_QUICK = {"id": "p2", "name": "Sonnet quick", "agent": "claude", "model": "sonnet", "effort": "low"}


def _mock(page: Page, presets: list) -> list:
    """Serve ``presets`` from /api/config; a POST carrying them edits the list."""
    store = list(presets)

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
    """Open the ``combo`` dropdown ('coding' + 'Preset' or 'Model') and tap an option."""
    page.locator(f"#{combo}Btn").click()
    page.locator(f'#{combo}Menu [role="option"][data-value="{value}"]').click()


def test_a_preset_launches_with_its_model_and_effort(authed_page: Page, base_url: str) -> None:
    _mock(authed_page, [OPUS_DEEP, SONNET_QUICK])
    _open_coding(authed_page, base_url)
    trigger = authed_page.locator("#codingPresetBtn")
    expect(trigger).to_have_text("Default")

    # Default: today's behaviour, no effort on the launch.
    assert "effort" not in _launch(authed_page)

    _pick(authed_page, "codingPreset", "p1")
    expect(trigger).to_have_text("Opus deep")
    expect(authed_page.locator("#codingModelBtn")).to_contain_text("Opus")
    payload = _launch(authed_page)
    assert payload["model"] == "claude:opus"
    assert payload["effort"] == "xhigh"

    # A model picked by hand turns the preset off: its effort never rides on.
    _pick(authed_page, "codingModel", "claude:sonnet")
    expect(trigger).to_have_text("Default")
    payload = _launch(authed_page)
    assert payload["model"] == "claude:sonnet"
    assert "effort" not in payload

    # Another preset, then back to Default.
    _pick(authed_page, "codingPreset", "p2")
    payload = _launch(authed_page)
    assert (payload["model"], payload["effort"]) == ("claude:sonnet", "low")
    _pick(authed_page, "codingPreset", "default")
    expect(trigger).to_have_text("Default")
    assert "effort" not in _launch(authed_page)


def test_the_pick_is_remembered_on_the_device(authed_page: Page, base_url: str) -> None:
    _mock(authed_page, [OPUS_DEEP])
    _open_coding(authed_page, base_url)
    _pick(authed_page, "codingPreset", "p1")
    authed_page.reload(wait_until="domcontentloaded")
    authed_page.locator("details.projects-card").evaluate("el => { el.open = true; }")
    expect(authed_page.locator("#codingPresetBtn")).to_have_text("Opus deep")
    assert _launch(authed_page)["effort"] == "xhigh"


def test_no_preset_draws_nothing(authed_page: Page, base_url: str) -> None:
    _mock(authed_page, [])
    _open_coding(authed_page, base_url)
    expect(authed_page.locator("#codingModelBtn")).to_be_visible()
    expect(authed_page.locator("#codingPresetCombo")).to_have_count(0)


def test_settings_adds_renames_and_removes_a_preset(authed_page: Page, base_url: str) -> None:
    store = _mock(authed_page, [])
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    open_settings_sheet(authed_page, "launchDefaultsSheet")

    authed_page.locator("#launchPresetName").fill("Opus deep")
    authed_page.locator("#launchPresetModel").select_option("claude:opus")
    authed_page.locator("#launchPresetEffort").select_option("xhigh")
    authed_page.locator("#launchPresetAdd").click()
    row = authed_page.locator("#launchPresetList li")
    expect(row).to_have_count(1)
    expect(row.locator(".launch-preset-name")).to_have_value("Opus deep")
    expect(row.locator(".launch-preset-meta")).to_contain_text("Opus")
    assert [(p["name"], p["model"], p["effort"]) for p in store] == [("Opus deep", "opus", "xhigh")]

    name = row.locator(".launch-preset-name")
    name.fill("Opus max")
    name.press("Tab")
    expect(authed_page.locator("#launchPresetList .launch-preset-name")).to_have_value("Opus max")
    assert store[0]["name"] == "Opus max"

    row.locator(".action-row-kebab").click()
    authed_page.locator('[data-item="launch-preset-remove-item"]').click()
    expect(authed_page.locator("#launchPresetList li")).to_have_count(0)
    assert store == []
    close_settings_sheets(authed_page)
