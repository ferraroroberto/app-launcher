"""Regression pin for issue #212 (collapsible Code-tab panels).

The feature: the Code tab's **🟢 Running sessions** and **📁 Projects**
panels are each a collapsible ``<details>`` mirroring the **⚙️ Coding
options** card — open by default, with the ``›``→``⌄`` chevron on the
summary title. The ⎇ status / 🔄 refresh buttons live in the sessions
summary, so a tap there must drive the button only, never toggle the
panel (the same stopPropagation guard Coding options uses for its
Detached/Resume toggles).

Runs in both projections — the wiring is browser-agnostic but the iPhone
projection confirms the phone surface too.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.smoke


def _is_open(page: Page, selector: str) -> bool:
    return bool(page.locator(selector).evaluate("el => el.open"))


def test_sessions_and_projects_are_collapsible_open_by_default(
    authed_page: Page, base_url: str
) -> None:
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    # Both panels exist as <details> and start open.
    sessions = authed_page.locator("details.sessions-card")
    projects = authed_page.locator("details.projects-card")
    sessions.wait_for(state="attached", timeout=10_000)
    projects.wait_for(state="attached", timeout=10_000)
    assert _is_open(authed_page, "details.sessions-card"), "sessions panel should open by default"
    assert _is_open(authed_page, "details.projects-card"), "projects panel should open by default"

    # Tapping the summary title collapses, then re-expands the projects panel.
    title = authed_page.locator("details.projects-card .coding-summary-title")
    title.click()
    assert not _is_open(authed_page, "details.projects-card"), "title tap should collapse the panel"
    title.click()
    assert _is_open(authed_page, "details.projects-card"), "second title tap should re-expand it"


def test_header_action_tap_does_not_toggle_sessions_panel(
    authed_page: Page, base_url: str
) -> None:
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    sessions = authed_page.locator("details.sessions-card")
    sessions.wait_for(state="attached", timeout=10_000)
    assert _is_open(authed_page, "details.sessions-card")

    # The 🔄 refresh button sits inside the sessions <summary>; clicking it
    # must refresh sessions but leave the panel open (stopPropagation).
    authed_page.locator("#refreshSessions").click()
    assert _is_open(authed_page, "details.sessions-card"), (
        "refresh button tap must not collapse the sessions panel"
    )
