"""Regression pin for #1131: five tabs, one page header per tab, and Settings
one tap away.

Six tabs exceeded the five a bottom bar holds (HIG and Material agree),
which forced 11px labels and an icon-only fallback on narrow desktops.
Settings, the rarely used destination, was one of the six. Each tab also
opened with a different anatomy, and four of the six had no title at all.

Now Settings has no tab: a gear in every page header opens its pane, and
choosing any tab leaves it. Every pane opens with the vendored home-head,
titled after its tab.

Also carries the Coding tab's #496 home-head pins (items 1 + 2), formerly
``test_home_head.py`` — folded in by #1215 because both loaded the same page
with no mocks:

Item 1: the Coding tab opens with the vendored ``home-head`` summary card
as its first card — leading icon + bold "Launcher" title, a one-line
stats slot (sessions count at minimum), and the theme toggle pinned right
in the card's ``.home-toggle`` slot (same position as the other fleet
apps; behavior unchanged, covered by test_settings_tab).

Item 2: the launch-time Detached/Resume toggles moved onto the launcher
surface (the Projects card's summary), and the per-agent options card
dropped to the very bottom of the tab.

Runs in both projections — layout is CSS-driven and the iPhone projection
confirms the phone surface.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import stable_read

pytestmark = pytest.mark.smoke

# tab button -> (pane, header title)
_TABS = {
    "#tabBoard": ("#paneBoard", "Board"),
    "#tabClaude": ("#paneClaude", "Code"),
    "#tabLifeOS": ("#paneLifeOS", "Life"),
    "#tabApps": ("#paneApps", "Apps"),
    "#tabJobs": ("#paneJobs", "Jobs"),
}


def _assert_home_head_is_first_card_with_stats_and_toggle(page: Page) -> None:
    """#496 item 1. Formerly
    ``test_home_head.py::test_home_head_is_first_card_with_stats_and_toggle``."""
    head = page.locator("#paneClaude .home-head")
    expect(head).to_be_visible()
    # Since #1131 the header names its tab.
    expect(head.locator(".home-title")).to_contain_text("Code")

    # First card of the pane — the summary row leads the tab.
    first_class = page.evaluate(
        "document.getElementById('paneClaude').firstElementChild.className"
    )
    assert "home-head" in first_class, (
        f"home-head must be the pane's first card, got {first_class!r}"
    )

    # The stats line renders at least the sessions count once boot lands.
    expect(page.locator("#homeHeadStatus")).to_contain_text(
        "session", timeout=10_000
    )

    # Theme toggle sits inside the card (its behavior is pinned elsewhere).
    expect(head.locator("#themeToggle")).to_be_visible()

    # One-line contract: the card keeps the 52px closed-summary geometry.
    box = stable_read(head.bounding_box)
    assert box is not None
    assert box["height"] <= 60, (
        f"home-head should be a one-line 52px row, got {box['height']}px"
    )


def _assert_options_card_is_last_and_toggles_live_on_projects_card(
    page: Page,
) -> None:
    """#496 item 2. Formerly
    ``test_home_head.py::test_options_card_is_last_and_toggles_live_on_projects_card``."""
    # The options card is the LAST card on the Coding tab (#496 item 2).
    last_id = page.evaluate(
        "document.getElementById('paneClaude').lastElementChild.id"
    )
    assert last_id == "codingOptions", (
        f"options card must be the pane's last card, got {last_id!r}"
    )

    # Detached + Resume live on the Projects card — the surface sessions are
    # launched from — in its toolbar since #1132, not its <summary>.
    page.locator("details.projects-card").evaluate("el => { el.open = true; }")
    projects_toolbar = page.locator("details.projects-card .card-toolbar")
    expect(projects_toolbar.locator("#claudeDetached")).to_be_attached()
    expect(projects_toolbar.locator("#claudeResume")).to_be_attached()

    # A toggle tap flips the switch without expanding/collapsing the panel
    # (the stopPropagation guard rode along with the move).
    was_open = page.locator("details.projects-card").evaluate("el => el.open")
    page.locator("#claudeDetached").click()
    expect(page.locator("#claudeDetached")).to_have_attribute(
        "aria-checked", "true"
    )
    still_open = page.locator("details.projects-card").evaluate("el => el.open")
    assert still_open == was_open, (
        "Detached tap must not toggle the Projects panel"
    )
    # Leave the client-side switch off again for the rest of this test.
    page.locator("#claudeDetached").click()


def _assert_five_tabs_with_labels_at_320px(page: Page) -> None:
    """Formerly ``test_five_tabs_with_labels_at_320px``."""
    page.set_viewport_size({"width": 320, "height": 700})
    page.reload(wait_until="domcontentloaded")
    tabs = page.locator("nav.tabs > .tab")
    expect(tabs).to_have_count(5)
    for label in page.locator("nav.tabs .tab-label").all():
        expect(label).to_be_visible()


def test_every_pane_opens_with_its_header_and_a_settings_gear(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    page.goto(f"{base_url}/", wait_until="domcontentloaded")

    # The Coding tab is the default: pin its #496 anatomy before any tab moves.
    _assert_home_head_is_first_card_with_stats_and_toggle(page)
    _assert_options_card_is_last_and_toggles_live_on_projects_card(page)

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

    # Last: resizes and reloads the page.
    _assert_five_tabs_with_labels_at_320px(page)
