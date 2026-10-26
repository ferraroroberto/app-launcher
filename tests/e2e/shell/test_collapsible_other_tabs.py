"""Regression pin for issue #226 (collapsible Apps/Jobs/Life panels).

The feature: the Apps, Jobs and Life tabs' top-level panels are each a
collapsible ``<details>`` reusing the Code tab's ``.card--collapsible`` /
``.collapse-summary`` chrome, with the right-pinned chevron on the summary
title, so the whole app shares one foldable-section idiom.

Covered panels:
- Apps: Running and Apps (open by default since #1437) and Trays
  (collapsed by default, #383 review round).
- Jobs: the Jobs card (``details.jobs-card``, #1438) — the ➕ Add job button
  sits in the card's toolbar (always visible since Edit mode was removed) and
  a tap there must drive the button only, never the collapse.
- Life: 📚 Skills.

Runs in both projections — the wiring is browser-agnostic but the iPhone
projection confirms the phone surface too.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke


def _is_open(page: Page, selector: str) -> bool:
    return bool(page.locator(selector).evaluate("el => el.open"))


def test_apps_life_and_jobs_panels_are_collapsible(
    authed_page: Page, base_url: str
) -> None:
    """All three tabs' panels on one page load (#1215): Apps, then Life,
    then Jobs last — its Add-job step opens the modal ``#jobDialog``."""
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    # -- was test_apps_tab_panels_default_states --
    authed_page.locator("#tabApps").click()

    # #1437: Running and the Apps list open by default, Trays folded; the
    # Port listeners card is gone (its list is the Other ports sheet).
    for sel, should_open in (
        ("#paneApps details.running-apps-card", True),
        ("#paneApps details.apps-list-card", True),
        ("#paneApps details.registered-trays-card", False),
    ):
        panel = authed_page.locator(sel)
        panel.wait_for(state="attached", timeout=10_000)
        assert _is_open(authed_page, sel) is should_open, (
            f"{sel} default should be open={should_open} (#1437)"
        )
    expect(authed_page.locator("#paneApps details.listeners-card")).to_have_count(0)

    # Tapping the Apps summary title collapses, then re-expands it.
    title = authed_page.locator("#paneApps details.apps-list-card .collapse-title")
    title.click()
    assert not _is_open(authed_page, "#paneApps details.apps-list-card"), (
        "title tap should collapse the panel"
    )
    title.click()
    assert _is_open(authed_page, "#paneApps details.apps-list-card"), (
        "second title tap should expand it again"
    )

    # -- was test_life_skills_panel_is_collapsible --
    authed_page.locator("#tabLifeOS").click()

    skills = authed_page.locator("#paneLifeOS details.lifeos-list-card")
    skills.wait_for(state="attached", timeout=10_000)
    assert _is_open(authed_page, "#paneLifeOS details.lifeos-list-card"), (
        "skills panel should open by default"
    )

    title = authed_page.locator("#paneLifeOS details.lifeos-list-card .collapse-title")
    title.click()
    assert not _is_open(authed_page, "#paneLifeOS details.lifeos-list-card"), (
        "title tap should collapse the panel"
    )
    title.click()
    assert _is_open(authed_page, "#paneLifeOS details.lifeos-list-card"), (
        "second title tap should re-expand it"
    )

    # -- was test_jobs_panel_is_collapsible_and_add_button_does_not_toggle
    # (last: it opens the modal #jobDialog) --
    authed_page.locator("#tabJobs").click()

    jobs = authed_page.locator("#paneJobs details.jobs-card")
    jobs.wait_for(state="attached", timeout=10_000)
    assert _is_open(authed_page, "#paneJobs details.jobs-card"), (
        "jobs panel should open by default"
    )

    # The ➕ Add job button lives in the jobs toolbar and is always visible
    # since #1438 (Edit mode, which used to gate it, is gone).
    add_job = authed_page.locator("#jobsAddBtn")
    add_job.wait_for(state="visible", timeout=10_000)
    add_job.click()
    assert _is_open(authed_page, "#paneJobs details.jobs-card"), (
        "header action tap must not collapse the jobs panel"
    )
    assert bool(authed_page.locator("#jobDialog").evaluate("el => el.open")), (
        "Add job should open its dialog"
    )
