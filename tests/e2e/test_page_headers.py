"""Regression pin for #1131: five tabs, one page header per tab, and Settings
one tap away.

Six tabs exceeded the five a bottom bar holds (HIG and Material agree),
which forced 11px labels and an icon-only fallback on narrow desktops.
Settings, the rarely used destination, was one of the six. Each tab also
opened with a different anatomy, and four of the six had no title at all.

Now Settings has no tab: a gear in every page header opens its pane, and
choosing any tab leaves it. Every pane opens with the vendored home-head,
titled after its tab.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

# tab button -> (pane, header title)
_TABS = {
    "#tabBoard": ("#paneBoard", "Board"),
    "#tabClaude": ("#paneClaude", "Code"),
    "#tabLifeOS": ("#paneLifeOS", "Life"),
    "#tabApps": ("#paneApps", "Apps"),
    "#tabJobs": ("#paneJobs", "Jobs"),
}


def test_five_tabs_with_labels_at_320px(authed_page: Page, base_url: str) -> None:
    page = authed_page
    page.set_viewport_size({"width": 320, "height": 700})
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    tabs = page.locator("nav.tabs > .tab")
    expect(tabs).to_have_count(5)
    for label in page.locator("nav.tabs .tab-label").all():
        expect(label).to_be_visible()


def test_every_pane_opens_with_its_header_and_a_settings_gear(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    for tab, (pane, title) in _TABS.items():
        page.locator(tab).click()
        expect(page.locator(pane)).to_be_visible()
        first = page.evaluate(
            "(sel) => document.querySelector(sel).firstElementChild.className", pane
        )
        assert "home-head" in first, f"{pane} opens with {first!r}, not its page header"
        head = page.locator(f"{pane} > .home-head")
        expect(head.locator(".home-title")).to_have_text(title)
        expect(head.locator(".theme-toggle-btn")).to_be_visible()

        # One tap to Settings from here, and any tab leads back out.
        head.locator(".settings-open-btn").click()
        expect(page.locator("#paneSettings")).to_be_visible()
        expect(page.locator(pane)).to_be_hidden()
        expect(page.locator("nav.tabs .tab[aria-selected='true']")).to_have_count(0)
        page.locator(tab).click()
        expect(page.locator("#paneSettings")).to_be_hidden()
        expect(page.locator(pane)).to_be_visible()
        expect(page.locator(tab)).to_have_attribute("aria-selected", "true")
