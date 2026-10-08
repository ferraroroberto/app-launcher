"""Regression pin for #1434 (was #139's off-main status popover).

#139 put an off-main popover behind the Code tab's "Git status" button.
#1434 removed both: the rows themselves are the off-main list ("on
<branch>" in their meta), and the refresh is an icon button in the Projects
card's toolbar whose only feedback is the footer's "Git checked just now".
This pins that the refresh re-runs the git check, that no popover opens,
and that the old button is gone.

Hermetic: /api/claude-code/git-status is fulfilled with a canned empty body
before navigation, so the test counts requests instead of running git.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import wait_until

pytestmark = pytest.mark.smoke


def test_git_refresh_button_rechecks_without_a_popover(
    authed_page: Page, base_url: str
) -> None:
    calls: list[str] = []

    def _fulfill(route):
        calls.append(route.request.url)
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"projects": []}))

    authed_page.route("**/api/claude-code/git-status", _fulfill)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    wait_until(authed_page, lambda: len(calls) >= 1, "the boot git-status fetch")

    authed_page.locator("details.projects-card").evaluate("el => { el.open = true; }")
    refresh = authed_page.locator(".projects-toolbar #gitRefreshBtn")
    expect(refresh).to_be_visible()
    expect(refresh).to_have_attribute("aria-label", "Check git status of all projects")
    expect(authed_page.locator("#gitStatusBtn")).to_have_count(0)
    expect(authed_page.locator("#gitStatusSummary")).to_have_count(0)

    before = len(calls)
    refresh.click()
    wait_until(authed_page, lambda: len(calls) > before, "the refresh's git-status fetch")
    expect(authed_page.locator("#gitCheckedFooter")).to_have_text("Git checked just now")
    expect(refresh).to_be_enabled()
