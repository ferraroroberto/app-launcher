"""Settings tab (issue #383).

Settings moved from an always-visible collapsible card at the bottom of
every tab into a sixth navigation tab. Contract under test:

  * ``#tabSettings`` is a real tab: clicking it shows ``#paneSettings``
    with the settings controls and hides the other panes.
  * The settings card no longer bleeds into the other tabs — on the
    default Coding tab the panel is hidden.
  * The app theme toggle lives on the Coding tab (issue #392 moved it out
    of the Settings pane; #496 moved it into the home-head summary card's
    toggle slot) and still flips ``html[data-theme]``.

Issue #435 follow-up adds the terminal-scrollback-depth setting and removes
the TLS-badge / tunnel-URL status readout (needless exposure of the tunnel
hostname in the UI) — both covered below.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke


def test_settings_tab_opens_pane_with_controls(
    authed_page: Page, base_url: str
) -> None:
    authed_page.goto(base_url, wait_until="domcontentloaded")
    expect(authed_page.locator("#buildReadout")).to_contain_text(
        "Build:", timeout=10_000
    )

    authed_page.locator("#tabSettings").click()
    expect(authed_page.locator("#paneSettings")).to_be_visible()
    expect(authed_page.locator("#paneClaude")).to_be_hidden()
    expect(authed_page.locator("#tabSettings")).to_have_attribute(
        "aria-selected", "true"
    )

    # The settings controls render inside the pane, no disclosure to open.
    expect(authed_page.locator("#projectsDir")).to_be_visible()
    expect(authed_page.locator("#saveSettings")).to_be_visible()
    expect(authed_page.locator("#editMode")).to_be_visible()


def test_settings_panel_absent_from_other_tabs(
    authed_page: Page, base_url: str
) -> None:
    authed_page.goto(base_url, wait_until="domcontentloaded")
    # Default tab is Coding — the settings panel must not bleed through.
    expect(authed_page.locator("#settingsPanel")).to_be_hidden()
    authed_page.locator("#tabApps").click()
    expect(authed_page.locator("#settingsPanel")).to_be_hidden()


def test_theme_toggle_lives_on_coding_tab_and_flips_theme(
    authed_page: Page, base_url: str
) -> None:
    authed_page.goto(base_url, wait_until="domcontentloaded")
    toggle = authed_page.locator("#themeToggle")
    # Lives in the home-head card on the Coding tab (#496) — visible on load.
    expect(toggle).to_be_visible()
    # Not duplicated into the Settings pane.
    authed_page.locator("#tabSettings").click()
    expect(toggle).to_be_hidden()
    authed_page.locator("#tabClaude").click()
    expect(toggle).to_be_visible()

    before = authed_page.evaluate(
        "document.documentElement.dataset.theme || 'light'"
    )
    toggle.click()
    after = authed_page.evaluate(
        "document.documentElement.dataset.theme || 'light'"
    )
    assert after != before


def test_terminal_history_lines_field_loads_and_saves(
    authed_page: Page, base_url: str
) -> None:
    """The scrollback-depth field (issue #435 follow-up) loads pre-filled
    from GET /api/config and a new value survives a Save + page reload."""
    authed_page.goto(base_url, wait_until="domcontentloaded")
    authed_page.locator("#tabSettings").click()
    field = authed_page.locator("#terminalHistoryLines")
    expect(field).to_be_visible()
    # Pre-filled from the server default, not left blank.
    expect(field).not_to_have_value("")

    field.fill("5000")
    authed_page.locator("#saveSettings").click()
    # patchConfig() round-trips through GET /api/config on success.
    expect(field).to_have_value("5000")

    # Reload to confirm it actually persisted server-side, not just DOM.
    authed_page.goto(base_url, wait_until="domcontentloaded")
    authed_page.locator("#tabSettings").click()
    expect(authed_page.locator("#terminalHistoryLines")).to_have_value("5000")


def test_boot_autostart_toggle_writes_and_removes_startup_bat(
    authed_page: Page, base_url: str
) -> None:
    """The "Start app-launcher at log on" switch (issue #456 part 1/2) is a
    real filesystem side effect (a Startup-folder wrapper bat), not a plain
    config field — click it on, reload, and confirm the switch survives from
    a fresh GET /api/config (not just the optimistic click); then click it
    off and confirm the same across a reload."""
    authed_page.goto(base_url, wait_until="domcontentloaded")
    authed_page.locator("#tabSettings").click()
    toggle = authed_page.locator("#bootAutostartToggle")
    expect(toggle).to_be_visible()
    expect(toggle).to_have_attribute("aria-checked", "false")

    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "true")
    authed_page.goto(base_url, wait_until="domcontentloaded")
    authed_page.locator("#tabSettings").click()
    expect(authed_page.locator("#bootAutostartToggle")).to_have_attribute(
        "aria-checked", "true"
    )

    authed_page.locator("#bootAutostartToggle").click()
    expect(authed_page.locator("#bootAutostartToggle")).to_have_attribute(
        "aria-checked", "false"
    )
    authed_page.goto(base_url, wait_until="domcontentloaded")
    authed_page.locator("#tabSettings").click()
    expect(authed_page.locator("#bootAutostartToggle")).to_have_attribute(
        "aria-checked", "false"
    )


def test_settings_boolean_controls_use_vendored_switch(
    authed_page: Page, base_url: str
) -> None:
    """Settings booleans use the fleet switch track + sliding thumb."""
    authed_page.goto(base_url, wait_until="domcontentloaded")
    authed_page.locator("#tabSettings").click()

    for selector in ("#editMode", "#bootAutostartToggle"):
        toggle = authed_page.locator(selector)
        expect(toggle).to_have_class(re.compile(r"(?:^|\s)toggle(?:\s|$)"))
        expect(toggle.locator(".knob")).to_have_count(1)
        box = toggle.bounding_box()
        assert box is not None
        assert round(box["width"]) == 44
        assert round(box["height"]) == 26


def test_settings_status_readout_has_no_tls_or_tunnel_url(
    authed_page: Page, base_url: str
) -> None:
    """The TLS badge + tunnel-URL status line was removed (issue #435
    follow-up) — needless exposure of the tunnel hostname in the UI. Any
    reachability warning may still render; TLS/tunnel text must not."""
    authed_page.goto(base_url, wait_until="domcontentloaded")
    authed_page.locator("#tabSettings").click()
    readout = authed_page.locator("#statusReadout")
    text = (readout.text_content() or "").lower()
    assert "tls" not in text
    assert "tunnel" not in text
    assert "http" not in text
