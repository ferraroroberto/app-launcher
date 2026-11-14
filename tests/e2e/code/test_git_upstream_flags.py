"""Regression pin for #2414: ahead/behind chips, the pull-first gate, Sync all.

A project behind its remote carries a "behind N" chip apart from the dirty
chip, and launching it asks to pull first (one tap: Pull and launch). A
behind project that cannot be pulled (dirty, diverged) says why and offers
only "Launch anyway", which is the one way past the server's refusal. Sync
all lists every repo with its outcome.

Every endpoint is mocked before navigation with fictional projects, so no
real repo is fetched or pulled.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

_APPS = [
    {"id": p, "name": p, "kind": "claude-code", "project_dir": "", "added_at": "",
     "is_favorite": False, "repo_url": None}
    for p in ("alpha-site", "beta-tools")
]
_GIT = {"projects": [
    {"id": "alpha-site", "is_git": True, "branch": "main", "default_branch": "main",
     "on_default_branch": True, "dirty": False, "restart_needed": False,
     "upstream": {"state": "behind", "ahead": 0, "behind": 2, "reason": ""}},
    {"id": "beta-tools", "is_git": True, "branch": "main", "default_branch": "main",
     "on_default_branch": True, "dirty": True, "restart_needed": True,
     "upstream": {"state": "diverged", "ahead": 1, "behind": 3, "reason": ""}},
]}
_SYNC = {"results": [
    {"id": "alpha-site", "name": "alpha-site", "outcome": "pulled", "reason": "", "commits": 2,
     "restored": [], "kept_aside": []},
    {"id": "beta-tools", "name": "beta-tools", "outcome": "skipped", "reason": "dirty", "commits": 0,
     "restored": [], "kept_aside": []},
]}


def _fulfil(body: dict):
    return lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


def _mock(page: Page, calls: list) -> None:
    page.route("**/api/apps", _fulfil({"scan_root": "", "apps": _APPS}))
    page.route("**/api/apps/running", _fulfil({"running": []}))
    page.route("**/api/agents", _fulfil({"agents": [
        {"id": "claude", "label": "Claude Code", "available": True, "fullscreen": False},
    ], "vscode_available": False}))
    page.route(re.compile(r".*/api/claude-code/git-status(\?.*)?$"), _fulfil(_GIT))

    def record(body: dict):
        def handler(route):
            calls.append((route.request.url, route.request.post_data or ""))
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
        return handler

    page.route("**/api/claude-code/pull/*", record({"outcome": "pulled", "commits": 2, "reason": ""}))
    page.route("**/api/apps/*/launch", record({"launched": "x"}))
    page.route("**/api/claude-code/sync-all", record(_SYNC))
    page.route("**/api/ports/probe", lambda route: None)


def test_behind_chips_pull_first_gate_and_sync_all(authed_page: Page, base_url: str) -> None:
    page = authed_page
    calls: list = []
    _mock(page, calls)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("details.projects-card").evaluate("el => { el.open = true; }")

    alpha = page.locator('.coding-item[data-id="alpha-site"]')
    beta = page.locator('.coding-item[data-id="beta-tools"]')
    expect(alpha.locator(".git-behind")).to_have_text("behind 2")
    expect(alpha.locator(".git-behind")).to_have_attribute("data-tone", "attention")
    expect(alpha.locator(".git-uncommitted")).to_have_count(0)
    expect(beta.locator(".git-uncommitted")).to_have_text("uncommitted")
    expect(beta.locator(".git-diverged")).to_have_text("diverged")
    expect(beta.locator(".git-restart-needed")).to_have_text("restart needed")

    # A pullable behind repo: Pull and launch pulls, then launches.
    dialog = page.locator("#confirmDialog")
    alpha.locator(".action-row-main").click()
    expect(dialog.locator("#confirmDialogTitle")).to_have_text("Pull alpha-site first?")
    expect(dialog.locator("#confirmDialogMessage")).to_contain_text("2 commits behind origin/main")
    with page.expect_request("**/api/apps/alpha-site/launch") as launched:
        dialog.locator("#confirmDialogOk").click()
    assert "ignore_behind" not in (launched.value.post_data or "")
    # The pull was answered before the launch was even sent.
    assert any("/api/claude-code/pull/alpha-site" in u for u, _ in calls)

    # An unpullable one says why, and only "Launch anyway" sends ignore_behind.
    beta.locator(".action-row-main").click()
    expect(dialog.locator("#confirmDialogTitle")).to_have_text("beta-tools is behind")
    expect(dialog.locator("#confirmDialogMessage")).to_contain_text("uncommitted changes")
    expect(dialog.locator("#confirmDialogOk")).to_have_text("Launch anyway")
    with page.expect_request("**/api/apps/beta-tools/launch") as launched:
        dialog.locator("#confirmDialogOk").click()
    assert json.loads(launched.value.post_data or "{}").get("ignore_behind") is True

    # Sync all: one line per repo, its outcome as a chip.
    page.locator("#gitSyncAllBtn").click()
    sheet = page.locator("#gitSyncSheet")
    expect(sheet).to_be_visible()
    expect(sheet.locator('li[data-outcome="pulled"]')).to_contain_text("pulled 2")
    expect(sheet.locator('li[data-outcome="skipped"]')).to_contain_text("dirty")
    sheet.locator("#gitSyncDone").click()
    expect(sheet).not_to_be_visible()
