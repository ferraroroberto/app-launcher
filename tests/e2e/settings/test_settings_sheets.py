"""Settings as inset groups (#1435, step 3 of #1432).

Settings is five groups of rows (Activity, Appearance, Agents, Connections,
This PC). Each row shows its current value and a chevron and opens a sheet
on the vendored modal shell: a header back + ✕ and one full-width primary,
Done, except Screen time's Send to task-os (#1678). Contract:

  * the groups and rows render, and the values match the saved config
    (Launch defaults, Chief and Context filter at least);
  * every sheet opens and closes (back, ✕, Done, Escape), and Launch
    defaults opens one agent's sheet on top of itself;
  * every ``openSettingsAt`` caller lands inside the sheet that holds its
    field (the Code and Life empty states, the Code tab's chief row);
  * no Save button is left: fields save as they change.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._contrast import contrast_ratio
from tests.e2e._surface import surface
from tests.e2e.fixtures.pages import loopback
from tests.e2e.fixtures.sessions import auth_headers
from tests.e2e.fixtures.ui import (
    close_settings_sheets,
    open_agent_sheet,
    open_settings,
    open_settings_sheet,
)

pytestmark = pytest.mark.smoke

_GROUPS = ["Activity", "Appearance", "Agents", "Sessions", "Life OS", "Connections", "This PC"]
_SHEETS = [
    "screenTimeSheet", "usageShowsSheet", "launchDefaultsSheet", "chiefSheet",
    "channelsSheet", "contextFilterSheet", "fleetScopeSheet", "inspirationSheet", "tokensSheet", "foldersSheet",
    "passkeysSheet", "terminalSheet", "pcLoadSheet",
]
# The design inventory's name for each sheet (surface() wants lowercase).
_SURFACE_NAMES = {
    "screenTimeSheet": "screen-time", "usageShowsSheet": "usage-shows",
    "launchDefaultsSheet": "launch-defaults", "chiefSheet": "chief",
    "channelsSheet": "channels", "contextFilterSheet": "context-filter",
    "fleetScopeSheet": "fleet-scope", "inspirationSheet": "inspiration",
    "tokensSheet": "tokens", "foldersSheet": "folders",
    "passkeysSheet": "passkeys", "terminalSheet": "terminal", "pcLoadSheet": "pc-load",
}
# A sheet's one primary: Done, unless it has a job of its own.
_PRIMARY = {"screenTimeSheet": "Send to task-os"}
_AGENT_LABELS = {
    "claude": "Claude Code", "codex": "Codex", "antigravity": "Antigravity",
    "copilot": "GitHub Copilot", "pi": "Pi", "grok": "Grok Build",
}


def _config(base_url: str, auth_token: str) -> dict:
    resp = loopback("GET", base_url + "/api/config", headers=auth_headers(auth_token))
    resp.raise_for_status()
    return resp.json()


def _value(page: Page, key: str):
    return page.locator(f'#paneSettings [data-settings-value="{key}"]')


def _assert_groups_render_with_values_matching_the_saved_config(authed_page: Page, cfg: dict) -> None:
    expect(authed_page.locator("#paneSettings .settings-overline:visible")).to_have_text(_GROUPS)
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
    labels = {"claude": "Claude", "codex": "Codex", "grok": "Grok"}
    expect(_value(authed_page, "usage")).to_have_text(
        " · ".join(labels[p] for p in cfg["usage_providers"]) or "None"
    )
    claude = cfg["claude"]
    expect(authed_page.locator('#launchDefaultsSheet [data-settings-value="agent-claude"]')).to_contain_text(
        claude["model"].capitalize()
    )

    # The context filter's value is the mode its own sheet shows as active.
    open_settings_sheet(authed_page, "contextFilterSheet")
    active = authed_page.locator("#contextFilterMode .segmented-item[aria-pressed=true]")
    expect(active).to_have_count(1)
    mode = active.inner_text()
    close_settings_sheets(authed_page)
    expect(_value(authed_page, "context-filter")).to_have_text(mode)

    # Start at log on is the vendored switch, inline on its row.
    toggle = authed_page.locator("#paneSettings .settings-row--inline #bootAutostartToggle")
    expect(toggle).to_have_attribute("role", "switch")
    expect(toggle).to_have_class("toggle" if toggle.get_attribute("aria-checked") != "true" else "toggle on")


@pytest.mark.iphone
def test_every_sheet_opens_and_closes(authed_page: Page, base_url: str, auth_token: str) -> None:
    """The groups and rows render with the saved config's values, then every
    sheet opens and closes on the same page."""
    cfg = _config(base_url, auth_token)
    # The Voice group shows only in a Remote Desktop session; pin this PC's
    # session so the group list does not depend on how the box is reached.
    authed_page.route(
        "**/api/desktop-session",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"state": "local", "this_pc": true}'
        ),
    )
    authed_page.goto(base_url, wait_until="domcontentloaded")
    open_settings(authed_page)
    _assert_groups_render_with_values_matching_the_saved_config(authed_page, cfg)
    surface(authed_page, "settings.pane")

    for sheet_id in _SHEETS:
        sheet = authed_page.locator(f"#{sheet_id}")
        # Modal contract (#545): a header ✕ and one full-width primary.
        expect(sheet.locator(".detail-actions button")).to_have_count(1)
        expect(sheet.locator(".detail-actions .button-primary.detail-save-btn")).to_have_text(
            _PRIMARY.get(sheet_id, "Done"))
        open_settings_sheet(authed_page, sheet_id)
        surface(authed_page, f"settings.{_SURFACE_NAMES[sheet_id]}")
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
        surface(authed_page, f"settings.agent-{agent_id}")
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


_OPEN_SETTINGS_AT_CALLERS = [
    # The Code tab's chief row kebab item (#1434; the Board's gear went
    # with its chief card in #1436). The menu is in the document only
    # while open (#1565), so the kebab comes first. It leads, while the
    # Code tab still shows.
    (("#sessionsList li.session-item-chief .session-kebab",
      ".session-menu [data-item=chief-settings-btn]"), "chiefSheet", None),
    (("#claudeEmptyAction",), "foldersSheet", "projectsDir"),
    (("#lifeOsEmptyAction",), "foldersSheet", "lifeOsDir"),
]


def test_no_save_button_is_left_and_open_settings_at_callers_land_in_their_sheet(
    authed_page: Page, base_url: str
) -> None:
    """Every Settings field saves as it changes (#1435), so no Save button is
    left. Then each ``openSettingsAt`` caller is clicked through its own
    listener (a dispatched click: the empty states only show when their list
    is empty), closing the sheet it opened before the next."""
    authed_page.goto(base_url, wait_until="domcontentloaded")
    scope = authed_page.locator("#paneSettings button, dialog.settings-sheet button")
    texts = [t.strip() for t in scope.all_text_contents()]
    assert not [t for t in texts if t == "Save"], texts
    expect(authed_page.locator("#saveSettings, #saveChiefSettings, #chiefSettingsDialog, #codingOptions")).to_have_count(0)

    for trigger, sheet_id, field_id in _OPEN_SETTINGS_AT_CALLERS:
        for selector in trigger:
            expect(authed_page.locator(selector)).to_be_attached()
            authed_page.locator(selector).dispatch_event("click")
        expect(authed_page.locator("#paneSettings")).to_be_visible()
        expect(authed_page.locator(f"#{sheet_id}")).to_be_visible()
        expect(authed_page.locator("dialog.settings-sheet[open]")).to_have_count(1)
        if field_id:
            expect(authed_page.locator(f"#{field_id}")).to_be_focused()
        close_settings_sheets(authed_page)


def _route_passkeys(page: Page) -> dict:
    """Answer the passkey routes from memory; returns what each one saw."""
    status = {
        "configured": True, "enrollment_open": True, "enrollment_seconds_left": 60,
        "devices": [{"id": "dev-1", "label": "Test phone", "added_at": "2026-01-01", "last_used": None}],
    }
    seen: dict = {"deletes": [], "begins": [], "native": []}
    page.route("**/api/webauthn/status", lambda route: route.fulfill(json=status))
    page.route(
        "**/api/webauthn/devices/*",
        lambda route: (seen["deletes"].append(route.request.method), route.fulfill(json={"ok": True})),
    )
    page.route(
        "**/api/webauthn/enroll/begin",
        lambda route: (seen["begins"].append(route.request.post_data_json),
                       route.fulfill(status=400, json={"detail": "stop here"})),
    )
    page.on("dialog", lambda d: (seen["native"].append(d.message), d.dismiss()))
    return seen


def _assert_passkeys_list_remove_and_enrol(page: Page, seen: dict) -> None:
    """Passkeys moved out of General into their own sheet (#1435): the row
    counts the devices, and remove and enrol still reach their endpoints."""
    expect(_value(page, "passkeys")).to_have_text("1 device")

    open_settings_sheet(page, "passkeysSheet")
    expect(page.locator("#webauthnDevices li")).to_have_count(1)
    page.locator("#webauthnDevices li .icon-button").click()
    # Remove and the device name ask in the themed dialog (#1501), not the
    # browser's confirm() / prompt().
    expect(page.locator("#confirmDialogTitle")).to_have_text('Remove passkey "Test phone"?')
    page.locator("#confirmDialogOk").click()
    expect(page.locator("#toast")).to_contain_text("Removed Test phone")
    assert seen["deletes"] == ["DELETE"]

    page.locator("#enrollDeviceBtn").click()
    expect(page.locator("#confirmDialogTitle")).to_have_text("Name this device")
    field = page.locator("#confirmDialogInput")
    expect(field).to_be_visible()
    expect(field).to_have_value("iPhone")
    field.fill("Second phone")
    page.locator("#confirmDialogOk").click()
    expect(page.locator("#toast")).to_contain_text("Enrollment failed")
    assert seen["begins"] == [{"label": "Second phone"}]
    assert seen["native"] == [], f"a native dialog was raised: {seen['native']}"
    close_settings_sheets(page)


def _channel_profile(label: str, ready: bool) -> dict:
    return {
        "id": label.lower(), "label": label, "skill": "notes-daily",
        "skill_found": True, "env_present": ready, "running": False,
        "session_id": "",
    }


def _route_channels(page: Page) -> dict:
    """Serve the Telegram checks from a mutable payload."""
    channels = {
        "profiles": [_channel_profile("Alpha", True), _channel_profile("Beta", True)],
        "problems": [],
        "setup": {"file_present": True, "life_os_found": True, "bun": True,
                  "plugin": True, "skills": []},
    }
    page.route(re.compile(r".*/api/life-os/channels$"),
               lambda route: route.fulfill(json=channels))
    return channels


def _assert_telegram_reads_plain_and_only_exceptions_wear_a_chip(page: Page, channels: dict) -> None:
    """Settings › Telegram finishes the contract (#1506): a healthy check or
    profile is a plain value, the profiles file's path shows only in a
    failing check's fix-it line, the row counts profiles and turns attention
    only while one needs setup, and the checks' muted text holds 4.5:1 on
    the sheet in both themes."""
    value = _value(page, "telegram")
    expect(value).to_have_text("2 profiles")
    assert not value.evaluate("(el) => el.hasAttribute('data-tone')")

    open_settings_sheet(page, "channelsSheet")
    checks = page.locator("#channelChecks .channel-check")
    expect(checks.locator(".channel-check-value")).to_have_text(
        ["Found", "Found", "Installed", "Found"]
    )
    expect(checks.first).to_contain_text("Profiles file")
    expect(page.locator("#channelChecks .channel-check-chip")).to_have_count(0)
    expect(page.locator("#channelChecks")).not_to_contain_text("channel_profiles")
    profiles = page.locator("#channelProfileChecks .channel-check")
    expect(profiles.locator(".channel-check-value")).to_have_text(["Ready", "Ready"])
    expect(page.locator("#channelProfileChecks .channel-check-chip")).to_have_count(0)

    detail = page.locator("#channelProfileChecks .muted.small")
    for theme in ("light", "dark"):
        page.evaluate("(t) => { document.documentElement.dataset.theme = t; }", theme)
        ratio = contrast_ratio(detail)
        assert ratio >= 4.5, f"profile check text is {ratio:.2f}:1 in {theme}"

    # The exceptions: a missing profiles file names its path in the fix-it
    # line, and a profile that needs setup wears the attention chip.
    channels["setup"]["file_present"] = False
    channels["profiles"][1]["env_present"] = False
    page.locator("#channelRecheck").click()
    missing = checks.filter(has_text="Profiles file")
    expect(missing.locator(".channel-check-chip")).to_have_text("Missing")
    expect(missing.locator(".channel-check-chip")).to_have_class(
        re.compile(r"\bchannel-check-chip-attention\b")
    )
    expect(missing).to_contain_text("config/channel_profiles.json")
    for theme in ("light", "dark"):
        page.evaluate("(t) => { document.documentElement.dataset.theme = t; }", theme)
        ratio = contrast_ratio(missing.locator(".channel-check-chip"))
        assert ratio >= 4.5, f"the Missing chip is {ratio:.2f}:1 in {theme}"
    expect(profiles.nth(1).locator(".channel-check-chip")).to_have_text("Needs setup")
    close_settings_sheets(page)
    expect(value).to_have_text("1 needs setup")
    expect(value).to_have_attribute("data-tone", "attention")


def _assert_pc_load_saves_the_night_restart_switch_and_window(
    page: Page, base_url: str, auth_token: str
) -> None:
    """Off by default, 21:00-05:00; the switch and each time save as they change
    and the Settings row's value follows (the autouse fixture puts the config back)."""
    cfg = _config(base_url, auth_token)
    assert cfg["process_auto_restart"] is False
    assert (cfg["process_auto_restart_start"], cfg["process_auto_restart_end"]) == ("21:00", "05:00")
    expect(_value(page, "pc-load")).to_have_text("Manual")
    open_settings_sheet(page, "pcLoadSheet")
    toggle = page.locator("#processAutoRestartToggle")
    expect(toggle).to_have_attribute("aria-checked", "false")
    expect(page.locator("#processAutoRestartStart")).to_have_value("21:00")
    for time_field in ("#processAutoRestartStart", "#processAutoRestartEnd"):
        expect(page.locator(time_field)).to_have_css("height", "44px")
        expect(page.locator(time_field)).to_have_css("text-align", "start")
    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "true")
    page.locator("#processAutoRestartEnd").fill("06:30")
    page.locator("#processAutoRestartEnd").dispatch_event("change")
    expect(page.locator("#toast")).to_contain_text("Restart window end saved.")
    saved = _config(base_url, auth_token)
    assert saved["process_auto_restart"] is True and saved["process_auto_restart_end"] == "06:30"


@pytest.mark.iphone
def test_pc_load_passkeys_and_telegram_sheets(
    authed_page: Page, base_url: str, auth_token: str
) -> None:
    """Three sheets on one page load, each closed before the next opens:
    PC load saves as it changes, Passkeys remove and enrol, and Telegram reads
    plain with only its exceptions chipped (last: it flips the theme)."""
    page = authed_page
    passkeys = _route_passkeys(page)
    channels = _route_channels(page)
    page.goto(base_url, wait_until="domcontentloaded")
    open_settings(page)
    _assert_pc_load_saves_the_night_restart_switch_and_window(page, base_url, auth_token)
    surface(page, "settings.pc-load-on")
    close_settings_sheets(page)
    expect(_value(page, "pc-load")).to_have_text("Night restart 21:00–06:30")
    _assert_passkeys_list_remove_and_enrol(page, passkeys)
    _assert_telegram_reads_plain_and_only_exceptions_wear_a_chip(page, channels)


@pytest.mark.iphone
def test_chat_file_folders_add_remove_and_refuse(
    authed_page: Page, base_url: str, auth_token: str, tmp_path
) -> None:
    """Chat file folders (#1748): the Folders sheet lists the folders an agent
    may send Chat files from, adds one (live on the next send, no restart),
    removes it, says why a bad entry is refused and keeps the last row."""
    page = authed_page
    before = _config(base_url, auth_token)["outbox_scratch_roots"]
    drop = tmp_path / "Drop"
    drop.mkdir()
    page.goto(base_url, wait_until="domcontentloaded")
    open_settings_sheet(page, "foldersSheet")
    rows = page.locator("#outboxRootsList li")
    expect(rows).to_have_count(len(before))

    field = page.locator("#outboxRootInput")
    field.fill(str(drop))
    page.locator("#outboxRootAdd").click()
    expect(rows).to_have_count(len(before) + 1)
    expect(rows.last.locator(".outbox-root-path")).to_have_text(str(drop))
    expect(field).to_have_value("")
    assert _config(base_url, auth_token)["outbox_scratch_roots"] == [*before, str(drop)]

    # Every remove is a real 44px target; the path is mono.
    remove = rows.last.locator(".icon-button")
    box = remove.bounding_box()
    assert box and box["width"] >= 44 - 0.5 and box["height"] >= 44 - 0.5, box

    # A refusal reads in the server's own words and saves nothing.
    field.fill(r"\\host\share")
    page.locator("#outboxRootAdd").click()
    expect(page.locator("#outboxRootInputError")).to_contain_text("network and device paths")
    field.fill(str(tmp_path / "never-made"))
    page.locator("#outboxRootAdd").click()
    expect(page.locator("#outboxRootInputError")).to_contain_text("does not exist")
    field.fill(str(drop))
    page.locator("#outboxRootAdd").click()
    expect(page.locator("#outboxRootInputError")).to_contain_text("already in the list")
    expect(rows).to_have_count(len(before) + 1)

    remove.click()
    expect(rows).to_have_count(len(before))
    assert _config(base_url, auth_token)["outbox_scratch_roots"] == before

    if len(before) == 1:
        # The last folder cannot be removed: an empty list reads as unset.
        expect(rows.first.locator(".icon-button")).to_be_disabled()

