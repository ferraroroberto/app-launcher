"""A failed read never leaves a blank pane (#1501, design.md "Async data").

Apps, Life's skills and the Board each declare their read through one
async-state block (async-state.js): a first read that fails shows the error
with one Retry instead of an empty list, Retry reads again, and a later read
that fails keeps the last good list under a stale line instead of blanking it.

Each source is served by a mutable route, so the test flips one read from
good to failing without touching the real server's data.
"""
from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

_APPS = {"apps": [{"id": "demo-app", "name": "Demo app", "kind": "webapp",
                   "url": "https://127.0.0.1:9999/"}]}
_SKILLS = {"skills": [{"id": "demo-skill", "name": "demo-skill", "is_favorite": False}]}


def _flaky(page: Page, pattern: str, body: dict, current: dict, key: str) -> None:
    """Serve `body` while current[key] is true, a 500 while it is false."""
    def _handle(route):
        if current[key]:
            route.fulfill(status=200, content_type="application/json", body=_json.dumps(body))
        else:
            route.fulfill(status=500, content_type="application/json",
                          body='{"detail": "invented failure"}')
    page.route(re.compile(pattern), _handle)


def test_apps_first_failure_shows_retry_and_a_later_one_keeps_the_list(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    current = {"apps": False}
    _flaky(page, r".*/api/apps$", _APPS, current, "apps")
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabApps").click()

    state = page.locator("#appsState")
    expect(state).to_have_attribute("data-state", "error")
    expect(state.locator(".empty-state-message")).to_have_text("Could not load the apps.")
    retry = state.locator(".async-retry")
    expect(retry).to_have_text("Retry")

    current["apps"] = True
    retry.click()
    expect(state).to_be_hidden()
    expect(page.locator("#appsList li[data-id='demo-app']")).to_be_visible()

    # The 4 s poll fails from here on: the list stays, a stale line says so.
    current["apps"] = False
    expect(state).to_have_attribute("data-state", "stale", timeout=15_000)
    expect(state).to_have_text(re.compile(r"^Last updated \d\d:\d\d · live data unavailable$"))
    expect(page.locator("#appsList li[data-id='demo-app']")).to_be_visible()


def test_life_skills_first_failure_shows_retry_and_a_later_one_keeps_the_list(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    current = {"skills": False}
    _flaky(page, r".*/api/life-os/skills$", _SKILLS, current, "skills")
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabLifeOS").click()

    state = page.locator("#lifeOsState")
    expect(state).to_have_attribute("data-state", "error")
    expect(state.locator(".empty-state-message")).to_have_text("Could not load the skills.")
    current["skills"] = True
    state.locator(".async-retry").click()
    expect(state).to_be_hidden()
    expect(page.locator("#lifeOsList li[data-id='demo-skill']")).to_be_visible()

    # Opening the tab again reads again; this read fails.
    current["skills"] = False
    page.locator("#tabLifeOS").click()
    expect(state).to_have_attribute("data-state", "stale")
    expect(page.locator("#lifeOsList li[data-id='demo-skill']")).to_be_visible()


def test_board_first_failure_replaces_the_lanes_and_a_later_one_keeps_them(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    current = {"board": False}
    board = {
        "columns": {"backlog": [], "claude_turn": [], "your_turn": [], "other": [], "done": []},
        "github": {"fetched_at": None, "error": None},
        "live_sessions": {"available": True, "error": None},
    }
    _flaky(page, r".*/api/board(?:\?.*)?$", board, current, "board")
    page.route(re.compile(r".*/api/board/chief-plan$"), lambda route: route.fulfill(
        status=200, content_type="application/json", body='{"state": "empty"}'))
    page.route(re.compile(r".*/api/board/github/refresh$"), lambda route: route.fulfill(
        status=200, content_type="application/json", body='{}'))
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabBoard").click()

    state = page.locator("#boardFetchState")
    expect(state).to_have_attribute("data-state", "error")
    expect(state.locator(".empty-state-message")).to_have_text("Could not load the Board.")
    expect(page.locator("#boardColumns")).to_be_hidden()

    current["board"] = True
    state.locator(".async-retry").click()
    expect(state).to_be_hidden()
    expect(page.locator("#boardColumns")).to_be_visible()

    # The 5 s poll fails from here on: the lanes stay under a stale line.
    current["board"] = False
    expect(state).to_have_attribute("data-state", "stale", timeout=15_000)
    expect(page.locator("#boardColumns")).to_be_visible()
