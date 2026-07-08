"""Settings tab (issue #383).

Settings moved from an always-visible collapsible card at the bottom of
every tab into a sixth navigation tab. Contract under test:

  * ``#tabSettings`` is a real tab: clicking it shows ``#paneSettings``
    with the settings controls and hides the other panes.
  * The settings card no longer bleeds into the other tabs — on the
    default Coding tab the panel is hidden.
  * The app theme toggle lives in the Coding tab's options card (its
    original home — issue #392 moved it back out of the Settings pane)
    and still flips ``html[data-theme]``.
"""

from __future__ import annotations

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


def test_theme_toggle_lives_in_coding_options_and_flips_theme(
    authed_page: Page, base_url: str
) -> None:
    authed_page.goto(base_url, wait_until="domcontentloaded")
    toggle = authed_page.locator("#themeToggle")
    # Lives in the Coding options card (the default tab) — visible on load.
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
