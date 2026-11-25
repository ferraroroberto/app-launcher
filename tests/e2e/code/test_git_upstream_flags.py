"""Regression pin for #2414: ahead/behind chips, the pull-first gate, Sync all.

A project behind its remote carries a "behind N" chip apart from the dirty
chip, and launching it asks to pull first (one tap: Pull and launch). A
behind project that cannot be pulled (dirty, diverged) says why and offers
only "Launch anyway", which is the one way past the server's refusal. Sync
all counts the projects while it runs (dialog and toast), then reports every
repo with its outcome until Done.

Every endpoint is mocked before navigation with fictional projects, so no
real repo is fetched or pulled.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import _fake_clock
from tests.e2e._surface import surface
from tests.e2e.fixtures.waits import wait_until

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
_RESULTS = [
    {"id": "alpha-site", "name": "alpha-site", "outcome": "pulled", "reason": "", "commits": 2,
     "restored": [], "kept_aside": []},
    {"id": "beta-tools", "name": "beta-tools", "outcome": "skipped", "reason": "dirty", "commits": 0,
     "restored": [], "kept_aside": []},
    {"id": "cinder-cli", "name": "cinder-cli", "outcome": "up-to-date", "reason": "", "commits": 0,
     "restored": [], "kept_aside": []},
    {"id": "delta-lab", "name": "delta-lab", "outcome": "failed", "commits": 0,
     "reason": "fast-forward refused: local changes would be overwritten", "restored": [], "kept_aside": []},
]


def _run(done: int, finished: bool = False, error: str | None = None) -> dict:
    """The server's Sync all progress after ``done`` of four projects."""
    return {"run": {
        "started_at": "2026-11-25T10:00:00Z",
        "finished_at": "2026-11-25T10:00:09Z" if finished else None,
        "total": 4, "done": done, "error": error,
        "current": None if finished else _RESULTS[min(done, 3)]["name"],
        "results": _RESULTS[:done],
    }}


def _fulfil(body: dict):
    return lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


def _mock(page: Page, calls: list, sync: dict) -> None:
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
    # Sync all: the POST starts the run, each GET reads how far it is. `sync` is the
    # run the next answer carries; a test moves it on between clock jumps.
    def sync_answer(route):
        calls.append((route.request.url, route.request.method))
        route.fulfill(status=200, content_type="application/json", body=json.dumps(sync["run"]))

    page.route("**/api/claude-code/sync-all", sync_answer)
    page.route("**/api/ports/probe", lambda route: None)


def _check_pull_gate(page: Page, calls: list) -> None:
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


def _sync_polls(calls: list) -> int:
    return sum(1 for url, method in calls if url.endswith("/api/claude-code/sync-all") and method == "GET")


def _next_read(page: Page, calls: list) -> None:
    """Jump the fake clock past the dialog's poll and wait for the read it fires."""
    before = _sync_polls(calls)

    def landed() -> bool:
        _fake_clock.advance(page, 2000)
        return _sync_polls(calls) > before
    wait_until(page, landed, "the next Sync all read")


def _check_sync_all(page: Page, calls: list, sync: dict) -> None:
    """A slow Sync all shows "Syncing N of M · project" in the dialog and in a neutral toast
    that outlives the dialog; the report keeps fast-forwarded, current, skipped (with the
    reason) and failed (with its error) on screen until Done."""
    page.locator("#gitSyncAllBtn").click()
    sheet = page.locator("#gitSyncSheet")
    state = sheet.locator("#gitSyncState")
    toast = page.locator("#toast")
    expect(sheet).to_be_visible()
    expect(state).to_contain_text("Syncing 1 of 4 · alpha-site")
    expect(toast).to_have_text(re.compile(r"Syncing 1 of 4 · alpha-site"))
    expect(toast).not_to_have_class(re.compile(r"\berror\b"))
    surface(page, "code.git-sync-sheet")

    sync["run"] = _run(1)
    _next_read(page, calls)
    expect(state).to_contain_text("Syncing 2 of 4 · beta-tools")
    expect(toast).to_have_text(re.compile(r"Syncing 2 of 4 · beta-tools"))
    assert [m for _, m in calls if m == "POST"] == ["POST"], "one tap starts exactly one run"

    # Closing the dialog does not stop the count: the toast carries it.
    sheet.locator("#gitSyncClose").click()
    expect(sheet).not_to_be_visible()
    sync["run"] = _run(3)
    _next_read(page, calls)
    expect(toast).to_have_text(re.compile(r"Syncing 4 of 4 · delta-lab"))

    # A tap while it runs brings the dialog back instead of starting a second run.
    page.locator("#gitSyncAllBtn").click()
    expect(sheet).to_be_visible()
    expect(state).to_contain_text("Syncing 4 of 4 · delta-lab")
    assert [m for _, m in calls if m == "POST"] == ["POST"]

    # It ends: the report stays in the dialog, the toast sums up red for the failure.
    sync["run"] = _run(4, finished=True)
    _next_read(page, calls)
    expect(state).to_be_hidden()
    expected = "1 fast-forwarded · 1 already current · 1 skipped (dirty) · 1 failed: delta-lab"
    expect(sheet.locator("#gitSyncSummary")).to_have_text(expected)
    expect(toast).to_have_text(expected)
    expect(toast).to_have_class(re.compile(r"\berror\b"))
    # Failures lead, then skips, then fast-forwards, then what was current.
    outcomes = sheet.locator("#gitSyncBody li").evaluate_all("els => els.map(e => e.dataset.outcome)")
    assert outcomes == ["failed", "skipped", "pulled", "up-to-date"], outcomes
    expect(sheet.locator('li[data-outcome="pulled"]')).to_contain_text("pulled 2")
    expect(sheet.locator('li[data-outcome="skipped"]')).to_contain_text("dirty")
    expect(sheet.locator('li[data-outcome="failed"]')).to_contain_text("fast-forward refused")

    sheet.locator("#gitSyncDone").click()
    expect(sheet).not_to_be_visible()

    # A run the server ends with an error is an error state, not a report.
    sync["run"] = _run(2, finished=True, error="OSError: disk")
    page.locator("#gitSyncAllBtn").click()
    expect(state).to_contain_text("Sync all stopped: OSError: disk")
    expect(sheet.locator("#gitSyncBody li")).to_have_count(0)
    sheet.locator("#gitSyncDone").click()


def test_behind_chips_pull_first_gate_and_sync_all(authed_page: Page, base_url: str) -> None:
    page = authed_page
    _fake_clock.install(page)
    calls: list = []
    sync = {"run": _run(0)}
    _mock(page, calls, sync)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("details.projects-card").evaluate("el => { el.open = true; }")
    _check_pull_gate(page, calls)
    _check_sync_all(page, calls, sync)
