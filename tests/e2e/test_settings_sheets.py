"""Settings as inset groups (#1435, step 3 of #1432).

Settings is four groups of rows (Appearance, Agents, Connections, This PC).
Each row shows its current value and a chevron and opens a sheet on the
vendored modal shell: a header back + ✕ and one full-width Done. Contract:

  * the groups and rows render, and the values match the saved config
    (Launch defaults, Chief and Context filter at least);
  * every sheet opens and closes (back, ✕, Done, Escape), and Launch
    defaults opens one agent's sheet on top of itself;
  * every ``openSettingsAt`` caller lands inside the sheet that holds its
    field (the Code and Life empty states, Telegram setup, the Board's chief
    gear);
  * no Save button is left: fields save as they change.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import (
    _auth_headers,
    _loopback,
    close_settings_sheets,
    open_agent_sheet,
    open_settings,
    open_settings_sheet,
)

pytestmark = pytest.mark.smoke

_GROUPS = ["Appearance", "Agents", "Connections", "This PC"]
_SHEETS = [
    "usageShowsSheet", "launchDefaultsSheet", "chiefSheet", "channelsSheet",
    "contextFilterSheet", "tokensSheet", "foldersSheet", "passkeysSheet",
    "terminalSheet",
]
_AGENT_LABELS = {
    "claude": "Claude Code", "codex": "Codex", "antigravity": "Antigravity",
    "copilot": "GitHub Copilot", "pi": "Pi", "grok": "Grok Build",
}


def _config(base_url: str, auth_token: str) -> dict:
    resp = _loopback("GET", base_url + "/api/config", headers=_auth_headers(auth_token))
    resp.raise_for_status()
    return resp.json()


def _value(page: Page, key: str):
    return page.locator(f'#paneSettings [data-settings-value="{key}"]')


@pytest.mark.iphone
def test_groups_render_with_values_matching_the_saved_config(
    authed_page: Page, base_url: str, auth_token: str
) -> None:
    cfg = _config(base_url, auth_token)
    authed_page.goto(base_url, wait_until="domcontentloaded")
    open_settings(authed_page)

    expect(authed_page.locator("#paneSettings .settings-overline")).to_have_text(_GROUPS)
    expect(authed_page.locator("#paneSettings button.settings-row[data-sheet]")).to_have_count(len(_SHEETS))
    # Every row is at least the one-line row height and its value sits in
    # the row (a chevron row), never wrapped below it.
    for sheet_id in _SHEETS:
        row = authed_page.locator(f'#paneSettings [data-sheet="{sheet_id}"]')
        expect(row.locator(".settings-row-chevron")).to_have_count(1)
        box = row.bounding_box()
        assert box and box["height"] >= 52 - 0.5, (sheet_id, box)

    cap = cfg["chief_worker_cap"]
    expect(_value(authed_page, "chief")).to_have_text(
        f"{cfg['chief_model'].capitalize()} · {cap} {'worker' if cap == 1 else 'workers'}"
    )
    fav = cfg.get("coding_favorite_agent") or "claude"
    launch = _value(authed_page, "launch")
    expect(launch).to_contain_text(_AGENT_LABELS[fav])
    if fav == "claude":
        expect(launch).to_have_text(f"Claude Code · {cfg['claude']['model'].capitalize()}")
    expect(_value(authed_page, "terminal")).to_have_text(
        f"{cfg['terminal_history_lines']} lines · {cfg['large_upload_max_mb']} MB"
    )
    expect(_value(authed_page, "usage")).to_have_text(
        {"claude": "Claude", "codex": "Codex", "both": "Both", "none": "None"}[cfg["usage_shows"]]
    )
    claude = cfg["claude"]
    expect(authed_page.locator('#launchDefaultsSheet [data-settings-value="agent-claude"]')).to_contain_text(
        claude["model"].capitalize()
    )

    # The context filter's value is the mode its own sheet shows as active.
    open_settings_sheet(authed_page, "contextFilterSheet")
    active = authed_page.locator("#contextFilterMode .range-tab.active")
    expect(active).to_have_count(1)
    mode = active.inner_text()
    close_settings_sheets(authed_page)
    expect(_value(authed_page, "context-filter")).to_have_text(mode)

    # Start at log on is the vendored switch, inline on its row.
    toggle = authed_page.locator("#paneSettings .settings-row--inline #bootAutostartToggle")
    expect(toggle).to_have_attribute("role", "switch")
    expect(toggle).to_have_class("toggle" if toggle.get_attribute("aria-checked") != "true" else "toggle on")


@pytest.mark.iphone
def test_every_sheet_opens_and_closes(authed_page: Page, base_url: str) -> None:
    authed_page.goto(base_url, wait_until="domcontentloaded")
    open_settings(authed_page)
    for sheet_id in _SHEETS:
        sheet = authed_page.locator(f"#{sheet_id}")
        # Modal contract (#545): a header ✕ and one full-width primary.
        expect(sheet.locator(".detail-actions button")).to_have_count(1)
        expect(sheet.locator(".detail-actions .button-primary.detail-save-btn")).to_have_text("Done")
        open_settings_sheet(authed_page, sheet_id)
        sheet.locator("[data-sheet-back]").click()
        expect(sheet).to_be_hidden()
        open_settings_sheet(authed_page, sheet_id)
        sheet.locator(".detail-header .detail-close").click()
        expect(sheet).to_be_hidden()
        expect(authed_page.locator("#paneSettings")).to_be_visible()

    # Launch defaults opens one agent's sheet with only its group, under its
    # name; back and Escape return to Launch defaults, Done closes both.
    for agent_id, label in _AGENT_LABELS.items():
        open_agent_sheet(authed_page, agent_id)
        expect(authed_page.locator("#agentSheetTitle")).to_have_text(label)
        expect(authed_page.locator("#agentSheet [data-agent-group]:visible")).to_have_count(1)
        expect(authed_page.locator("#launchDefaultsSheet")).to_be_hidden()
        authed_page.locator("#agentSheet [data-sheet-back]").click()
        expect(authed_page.locator("#launchDefaultsSheet")).to_be_visible()
        close_settings_sheets(authed_page)
    open_agent_sheet(authed_page, "codex")
    authed_page.keyboard.press("Escape")
    expect(authed_page.locator("#agentSheet")).to_be_hidden()
    expect(authed_page.locator("#launchDefaultsSheet")).to_be_visible()
    authed_page.keyboard.press("Escape")
    expect(authed_page.locator("dialog.settings-sheet[open]")).to_have_count(0)
    expect(authed_page.locator("#paneSettings")).to_be_visible()


@pytest.mark.parametrize(
    "trigger, sheet_id, field_id",
    [
        ("#claudeEmptyAction", "foldersSheet", "projectsDir"),
        ("#lifeOsEmptyAction", "foldersSheet", "lifeOsDir"),
        ("#lifeOsChannelsEmptyAction", "channelsSheet", None),
        # The Code tab's chief row kebab item (#1434; the Board's gear went
        # with its chief card in #1436).
        ("#sessionsList .chief-settings-btn", "chiefSheet", None),
    ],
)
def test_open_settings_at_callers_land_in_their_sheet(
    authed_page: Page, base_url: str, trigger: str, sheet_id: str, field_id: str | None
) -> None:
    """Each caller is clicked through its own listener (a dispatched click:
    the empty states only show when their list is empty)."""
    authed_page.goto(base_url, wait_until="domcontentloaded")
    expect(authed_page.locator(trigger)).to_be_attached()
    authed_page.locator(trigger).dispatch_event("click")
    expect(authed_page.locator("#paneSettings")).to_be_visible()
    expect(authed_page.locator(f"#{sheet_id}")).to_be_visible()
    expect(authed_page.locator("dialog.settings-sheet[open]")).to_have_count(1)
    if field_id:
        expect(authed_page.locator(f"#{field_id}")).to_be_focused()


def test_no_save_button_is_left(authed_page: Page, base_url: str) -> None:
    """Every Settings field saves as it changes (#1435)."""
    authed_page.goto(base_url, wait_until="domcontentloaded")
    scope = authed_page.locator("#paneSettings button, dialog.settings-sheet button")
    texts = [t.strip() for t in scope.all_text_contents()]
    assert not [t for t in texts if t == "Save"], texts
    expect(authed_page.locator("#saveSettings, #saveChiefSettings, #chiefSettingsDialog, #codingOptions")).to_have_count(0)


def test_passkeys_sheet_lists_removes_and_enrols(authed_page: Page, base_url: str) -> None:
    """Passkeys moved out of General into their own sheet (#1435): the row
    counts the devices, and remove and enrol still reach their endpoints."""
    page = authed_page
    status = {
        "configured": True, "enrollment_open": True, "enrollment_seconds_left": 60,
        "devices": [{"id": "dev-1", "label": "Test phone", "added_at": "2026-01-01", "last_used": None}],
    }
    page.route("**/api/webauthn/status", lambda route: route.fulfill(json=status))
    deletes: list = []
    page.route(
        "**/api/webauthn/devices/*",
        lambda route: (deletes.append(route.request.method), route.fulfill(json={"ok": True})),
    )
    begins: list = []
    page.route(
        "**/api/webauthn/enroll/begin",
        lambda route: (begins.append(route.request.post_data_json),
                       route.fulfill(status=400, json={"detail": "stop here"})),
    )
    page.on("dialog", lambda d: d.accept("Second phone") if d.type == "prompt" else d.accept())
    page.goto(base_url, wait_until="domcontentloaded")
    open_settings(page)
    expect(_value(page, "passkeys")).to_have_text("1 device")

    open_settings_sheet(page, "passkeysSheet")
    expect(page.locator("#webauthnDevices li")).to_have_count(1)
    page.locator("#webauthnDevices li .icon-button").click()
    expect(page.locator("#toast")).to_contain_text("Removed Test phone")
    assert deletes == ["DELETE"]

    page.locator("#enrollDeviceBtn").click()
    expect(page.locator("#toast")).to_contain_text("Enrollment failed")
    assert begins == [{"label": "Second phone"}]
