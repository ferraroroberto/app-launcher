"""Regression pin for issue #496 items 1 + 2 (home-head + card order).

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

pytestmark = pytest.mark.smoke


def test_home_head_is_first_card_with_stats_and_toggle(
    authed_page: Page, base_url: str
) -> None:
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    head = authed_page.locator("#paneClaude .home-head")
    expect(head).to_be_visible()
    expect(head.locator(".home-title")).to_contain_text("Launcher")

    # First card of the pane — the summary row leads the tab.
    first_class = authed_page.evaluate(
        "document.getElementById('paneClaude').firstElementChild.className"
    )
    assert "home-head" in first_class, (
        f"home-head must be the pane's first card, got {first_class!r}"
    )

    # The stats line renders at least the sessions count once boot lands.
    expect(authed_page.locator("#homeHeadStatus")).to_contain_text(
        "session", timeout=10_000
    )

    # Theme toggle sits inside the card (its behavior is pinned elsewhere).
    expect(head.locator("#themeToggle")).to_be_visible()

    # One-line contract: the card keeps the 52px closed-summary geometry.
    box = head.bounding_box()
    assert box is not None
    assert box["height"] <= 60, (
        f"home-head should be a one-line 52px row, got {box['height']}px"
    )


def test_options_card_is_last_and_toggles_live_on_projects_card(
    authed_page: Page, base_url: str
) -> None:
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    # The options card is the LAST card on the Coding tab (#496 item 2).
    last_id = authed_page.evaluate(
        "document.getElementById('paneClaude').lastElementChild.id"
    )
    assert last_id == "codingOptions", (
        f"options card must be the pane's last card, got {last_id!r}"
    )

    # Detached + Resume moved into the Projects card's summary — the surface
    # sessions are launched from — and stay functional switches there.
    projects_summary = authed_page.locator(
        "details.projects-card summary"
    )
    expect(projects_summary.locator("#claudeDetached")).to_be_attached()
    expect(projects_summary.locator("#claudeResume")).to_be_attached()

    # A toggle tap flips the switch without expanding/collapsing the panel
    # (the stopPropagation guard rode along with the move).
    was_open = authed_page.locator("details.projects-card").evaluate(
        "el => el.open"
    )
    authed_page.locator("#claudeDetached").click()
    expect(authed_page.locator("#claudeDetached")).to_have_attribute(
        "aria-checked", "true"
    )
    still_open = authed_page.locator("details.projects-card").evaluate(
        "el => el.open"
    )
    assert still_open == was_open, (
        "Detached tap must not toggle the Projects panel"
    )
    # Leave the client-side switch off for the next test's page.
    authed_page.locator("#claudeDetached").click()
