"""Regression pin for issue #226 (collapsible Apps/Jobs/Life panels).

The feature: the Apps, Jobs and Life tabs' top-level panels are each a
collapsible ``<details>`` reusing the Code tab's ``.coding-card`` /
``.coding-summary`` chrome — open by default, with the ``›``→``⌄`` chevron
on the summary title, so the whole app shares one foldable-section idiom.

Covered panels:
- Apps: 🟢 Running apps, 🔌 Port listeners, 📦 Registered apps.
- Jobs: 📋 Registered jobs — the ➕ Add job button sits in the summary row
  and a tap there must drive the button only (stopPropagation), never the
  collapse.
- Life: 📚 Skills.

Runs in both projections — the wiring is browser-agnostic but the iPhone
projection confirms the phone surface too.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.smoke


def _is_open(page: Page, selector: str) -> bool:
    return bool(page.locator(selector).evaluate("el => el.open"))


def test_apps_tab_panels_are_collapsible_open_by_default(
    authed_page: Page, base_url: str
) -> None:
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabApps").click()

    for sel in (
        "#paneApps details.sessions-card",
        "#paneApps details.listeners-card",
        "#paneApps details.apps-list-card",
    ):
        panel = authed_page.locator(sel)
        panel.wait_for(state="attached", timeout=10_000)
        assert _is_open(authed_page, sel), f"{sel} should open by default"

    # Tapping the Registered-apps summary title collapses, then re-expands it.
    title = authed_page.locator("#paneApps details.apps-list-card .coding-summary-title")
    title.click()
    assert not _is_open(authed_page, "#paneApps details.apps-list-card"), (
        "title tap should collapse the panel"
    )
    title.click()
    assert _is_open(authed_page, "#paneApps details.apps-list-card"), (
        "second title tap should re-expand it"
    )


def test_jobs_panel_is_collapsible_and_add_button_does_not_toggle(
    authed_page: Page, base_url: str
) -> None:
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabJobs").click()

    jobs = authed_page.locator("#paneJobs details.jobs-card")
    jobs.wait_for(state="attached", timeout=10_000)
    assert _is_open(authed_page, "#paneJobs details.jobs-card"), (
        "jobs panel should open by default"
    )

    # The ➕ Add job button lives in the jobs <summary>; it's revealed by Edit
    # mode. Reveal it, click it, and confirm the panel stays open
    # (stopPropagation) — the click opens the dialog instead of folding.
    authed_page.evaluate("document.getElementById('jobsAddBtn').hidden = false")
    authed_page.locator("#jobsAddBtn").click()
    assert _is_open(authed_page, "#paneJobs details.jobs-card"), (
        "header action tap must not collapse the jobs panel"
    )


def test_life_skills_panel_is_collapsible(authed_page: Page, base_url: str) -> None:
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabLifeOS").click()

    skills = authed_page.locator("#paneLifeOS details.lifeos-list-card")
    skills.wait_for(state="attached", timeout=10_000)
    assert _is_open(authed_page, "#paneLifeOS details.lifeos-list-card"), (
        "skills panel should open by default"
    )

    title = authed_page.locator("#paneLifeOS details.lifeos-list-card .coding-summary-title")
    title.click()
    assert not _is_open(authed_page, "#paneLifeOS details.lifeos-list-card"), (
        "title tap should collapse the panel"
    )
    title.click()
    assert _is_open(authed_page, "#paneLifeOS details.lifeos-list-card"), (
        "second title tap should re-expand it"
    )
