"""Regression pin for #1132: long lists get a filter, and no <summary> holds a
control.

The opened Projects, Apps and Jobs panes ran seven to eight phone screens,
with no way to narrow Projects or Apps, and a Jobs search that matched run
output but never a job's name. Separately, the Projects card's 52px <summary>
carried a model picker, three 36px buttons and its chevron, so a near-miss
folded the card. The same was true of Running sessions, Registered jobs and
Skills.
"""
from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.test_jobs_sort_next_run import _wire_jobs

pytestmark = pytest.mark.smoke

_PROJECTS = ["alpha-site", "beta-tools", "gamma-lab", "photo-ocr"]
_APPS = ["Photo OCR", "Voice Transcriber", "Reporting"]


def _json_route(page: Page, pattern, body: dict) -> None:
    page.route(
        re.compile(pattern) if isinstance(pattern, str) and pattern.startswith(".*") else pattern,
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(body)),
    )


def _mock(page: Page) -> None:
    apps = [
        {"id": p, "name": p, "kind": "claude-code", "project_dir": "", "added_at": "",
         "is_favorite": False, "repo_url": None}
        for p in _PROJECTS
    ] + [
        {"id": n.lower().replace(" ", "-"), "name": n, "kind": "streamlit", "bat_path": "",
         "added_at": "2026-01-01T00:00:00", "autostart": False}
        for n in _APPS
    ]
    _json_route(page, "**/api/apps", {"scan_root": "", "apps": apps})
    _json_route(page, "**/api/apps/running", {"running": []})
    _json_route(page, "**/api/agents", {"agents": [
        {"id": "claude", "label": "Claude Code", "available": True, "fullscreen": False},
    ], "vscode_available": False})
    _json_route(page, r".*/api/claude-code/git-status$", {"projects": []})
    _wire_jobs(page)


def _open_all(page: Page) -> None:
    page.evaluate("document.querySelectorAll('details').forEach((d) => { d.open = true; })")


def test_no_summary_holds_a_control(authed_page: Page, base_url: str) -> None:
    page = authed_page
    _mock(page)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    expect(page.locator("#claudeList .action-row").first).to_be_attached(timeout=5_000)
    offenders = page.evaluate(
        """() => Array.from(document.querySelectorAll('summary'))
            .filter((s) => s.querySelector('button, select, input, textarea, a[href]'))
            .map((s) => (s.textContent || '').trim().slice(0, 40))"""
    )
    assert not offenders, f"<summary> elements still holding a control: {offenders}"


def _visible_titles(page: Page, rows: str):
    return page.locator(f"{rows}:visible .action-row-title").all_inner_texts()


def test_projects_and_apps_filters_narrow_restore_and_empty(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    # Start from no stored filter, once: the reload below must keep it.
    page.add_init_script(
        "try { if (!sessionStorage.getItem('filters-reset')) {"
        " localStorage.removeItem('app-launcher.filter.projects');"
        " localStorage.removeItem('app-launcher.filter.apps');"
        " sessionStorage.setItem('filters-reset', '1'); } } catch (e) {}"
    )
    _mock(page)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    _open_all(page)

    rows = "#claudeList > li"
    expect(page.locator(rows)).to_have_count(len(_PROJECTS), timeout=5_000)
    box = page.locator("#claudeFilterInput")
    box.fill("oto")
    expect(page.locator(f"{rows}:visible")).to_have_count(1)
    assert _visible_titles(page, rows) == ["photo-ocr"]
    box.fill("zzz")
    expect(page.locator(f"{rows}:visible")).to_have_count(0)
    expect(page.locator("#claudeFilterEmpty")).to_be_visible()
    # The no-match state's own action clears the filter (#1238 J-09).
    page.locator("#claudeFilterEmpty .empty-state-action").click()
    expect(box).to_have_value("")
    expect(page.locator(f"{rows}:visible")).to_have_count(len(_PROJECTS))
    expect(page.locator("#claudeFilterEmpty")).to_be_hidden()

    # The query persists per list across a reload.
    box.fill("bet")
    page.reload(wait_until="domcontentloaded")
    _open_all(page)
    expect(page.locator("#claudeFilterInput")).to_have_value("bet")
    expect(page.locator(f"{rows}:visible")).to_have_count(1, timeout=5_000)

    page.locator("#tabApps").click()
    _open_all(page)
    apps = "#appsList > li"
    expect(page.locator(apps)).to_have_count(len(_APPS), timeout=5_000)
    page.locator("#appsFilterInput").fill("voi")
    expect(page.locator(f"{apps}:visible")).to_have_count(1)
    assert _visible_titles(page, apps) == ["Voice Transcriber"]
    page.locator("#appsFilterInput").fill("qqq")
    expect(page.locator("#appsFilterEmpty")).to_be_visible()
    page.locator("#appsFilterEmpty .empty-state-action").click()
    expect(page.locator(f"{apps}:visible")).to_have_count(len(_APPS))


def test_jobs_search_matches_job_names(authed_page: Page, base_url: str) -> None:
    page = authed_page
    _mock(page)
    page.route(
        re.compile(r".*/api/jobs/runs/search.*"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps({"matches": []})),
    )
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabJobs").click()
    _open_all(page)
    expect(page.locator("#jobsList li.app-item[data-id]")).to_have_count(3, timeout=5_000)

    page.locator("#jobsSearchInput").fill("man")
    expect(page.locator("#jobsList li.app-item[data-id]")).to_have_count(1, timeout=5_000)
    expect(page.locator("#jobsList li.app-item[data-id='mango']")).to_be_visible()

    page.locator("#jobsSearchInput").fill("nothing-matches")
    expect(page.locator("#jobsFilterEmpty")).to_be_visible(timeout=5_000)
    expect(page.locator("#jobsList li.app-item[data-id]")).to_have_count(0)
    page.locator("#jobsFilterEmptyAction").click()
    expect(page.locator("#jobsSearchInput")).to_have_value("")
    expect(page.locator("#jobsFilterEmpty")).to_be_hidden()
