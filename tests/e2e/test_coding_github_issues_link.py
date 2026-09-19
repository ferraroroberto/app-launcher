"""Regression pin for issue #297 (Coding tab GitHub entry → open-issues link).

The feature: the Coding tab's GitHub affordance used to open the bare repo
root (`a.repo_url`). It now opens that repo's open-issues list sorted by
last updated instead, since that's the page actually worth a tap from the
launcher. Issue #341 added `-label:audit-meta` to the query so codebase-audit
ledger/metadata issues (not actionable work) don't head the list.

#1070 moved it off the row and into the row's ⋯ menu — the URL contract and
the no-remote disabled state are unchanged, only where you tap to reach it.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke


def _row(slug: str, repo_url: str | None) -> dict:
    return {
        "id": slug,
        "name": slug,
        "kind": "claude-code",
        "project_dir": f"E:/automation/{slug}",
        "added_at": "",
        "is_favorite": False,
        "repo_url": repo_url,
    }


def _install_routes(page: Page, repo_url: str | None) -> None:
    def _apps(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {"scan_root": "E:/automation", "apps": [_row("alpha", repo_url)]}
            ),
        )

    page.route("**/api/apps", _apps)


def _open_projects(page: Page) -> None:
    # Projects is collapsed by default (#383 review round) — expand it so
    # the tile buttons are clickable.
    page.locator("details.projects-card").evaluate("el => { el.open = true; }")


def _github_item(page: Page):
    """GitHub's row in the ⋯ menu, with the menu opened (#1070)."""
    anchor = page.locator('.coding-item[data-id="alpha"] .project-menu-anchor')
    expect(anchor).to_be_enabled(timeout=5_000)
    anchor.click()
    menu = page.locator('.coding-item[data-id="alpha"] .project-menu')
    expect(menu).to_be_visible()
    return menu.locator(".project-github-btn")


def test_github_icon_opens_open_issues_sorted_by_updated(
    authed_page: Page, base_url: str
) -> None:
    repo_url = "https://github.com/ferraroroberto/app-launcher"
    _install_routes(authed_page, repo_url)
    # Capture window.open before the SPA loads — don't actually navigate.
    authed_page.add_init_script(
        "window.__opened = [];"
        "window.open = function (u) { window.__opened.push(u); return null; };"
    )
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_projects(authed_page)

    expect(authed_page.locator("#claudeList .coding-item")).to_have_count(1)
    gh_btn = _github_item(authed_page)
    expect(gh_btn).to_be_enabled()
    gh_btn.click()

    opened = authed_page.evaluate("window.__opened")
    expected = (
        repo_url
        + "/issues?q=is%3Aissue%20state%3Aopen%20sort%3Aupdated-desc%20-label%3Aaudit-meta"
    )
    assert opened == [expected], f"window.open called with {opened!r}, expected [{expected!r}]"


def test_github_icon_disabled_without_repo_url(authed_page: Page, base_url: str) -> None:
    _install_routes(authed_page, None)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_projects(authed_page)

    expect(authed_page.locator("#claudeList .coding-item")).to_have_count(1)
    expect(_github_item(authed_page)).to_be_disabled()
