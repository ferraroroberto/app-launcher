"""Regression pin for issue #297 (Coding tab GitHub icon → open-issues link).

The feature: the Coding tab's GitHub icon used to open the bare repo root
(`a.repo_url`). It now opens that repo's open-issues list sorted by last
updated instead, since that's the page actually worth a tap from the
launcher.
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

    expect(authed_page.locator("#claudeList .coding-item")).to_have_count(1)
    gh_btn = authed_page.locator('.coding-item[data-id="alpha"] .agent-btn').filter(
        has=authed_page.locator('img[alt="GitHub"]')
    )
    expect(gh_btn).to_be_enabled(timeout=5_000)
    gh_btn.click()

    opened = authed_page.evaluate("window.__opened")
    expected = repo_url + "/issues?q=is%3Aissue%20state%3Aopen%20sort%3Aupdated-desc"
    assert opened == [expected], f"window.open called with {opened!r}, expected [{expected!r}]"


def test_github_icon_disabled_without_repo_url(authed_page: Page, base_url: str) -> None:
    _install_routes(authed_page, None)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")

    expect(authed_page.locator("#claudeList .coding-item")).to_have_count(1)
    gh_btn = authed_page.locator('.coding-item[data-id="alpha"] .agent-btn').filter(
        has=authed_page.locator('img[alt="GitHub"]')
    )
    expect(gh_btn).to_be_disabled()
