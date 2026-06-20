"""Regression pin for issue #263 — responsive primary navigation.

The launcher keeps one DOM nav: top segmented control on desktop/fine-pointer
screens, floating bottom tab bar on the iPhone projection. The same test also
pins the tab ARIA state and the terminal-overlay hide rule for the mobile bar.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke


def test_primary_nav_is_responsive_and_accessible(
    authed_page: Page, base_url: str, browser_name: str
) -> None:
    authed_page.goto(base_url, wait_until="domcontentloaded")
    expect(authed_page.locator("#buildReadout")).to_contain_text(
        "Build:", timeout=10_000
    )

    tabs = authed_page.locator("nav.tabs")
    expect(tabs).to_be_visible()
    expect(tabs).to_have_attribute("role", "tablist")

    authed_page.locator("#tabJobs").click()
    expect(authed_page.locator("#paneJobs")).to_be_visible()
    expect(authed_page.locator("#paneClaude")).to_be_hidden()
    expect(authed_page.locator("#tabJobs")).to_have_attribute(
        "aria-selected", "true"
    )
    expect(authed_page.locator("#tabClaude")).to_have_attribute(
        "aria-selected", "false"
    )
    expect(tabs).to_have_attribute("data-active-tab", "jobs")

    metrics = authed_page.evaluate(
        """() => {
          const nav = document.querySelector('nav.tabs');
          const app = document.querySelector('.app');
          const icon = document.querySelector('#tabJobs .tab-icon');
          const navStyle = getComputedStyle(nav);
          const appStyle = getComputedStyle(app);
          const iconStyle = getComputedStyle(icon);
          const rect = nav.getBoundingClientRect();
          return {
            position: navStyle.position,
            display: navStyle.display,
            bottom: navStyle.bottom,
            iconDisplay: iconStyle.display,
            paddingBottom: parseFloat(appStyle.paddingBottom),
            rectBottom: rect.bottom,
            rectTop: rect.top,
            viewportHeight: window.innerHeight,
          };
        }"""
    )

    if browser_name == "webkit":
        assert metrics["position"] == "fixed"
        assert metrics["display"] == "grid"
        assert metrics["iconDisplay"] == "block"
        assert metrics["paddingBottom"] >= 80
        assert metrics["rectBottom"] <= metrics["viewportHeight"]
        assert metrics["rectTop"] > metrics["viewportHeight"] / 2

        authed_page.evaluate(
            """() => {
              document.getElementById('terminalOverlay').hidden = false;
              document.body.classList.add('terminal-open');
            }"""
        )
        expect(tabs).to_be_hidden()
    else:
        assert metrics["position"] in {"static", "relative"}
        assert metrics["display"] == "flex"
        assert metrics["iconDisplay"] == "none"
        assert metrics["paddingBottom"] < 100
