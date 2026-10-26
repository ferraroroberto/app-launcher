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

from tests.e2e.fixtures.waits import flush_requests, wait_until

pytestmark = pytest.mark.smoke

_APPS = {"apps": [{"id": "demo-app", "name": "Demo app", "kind": "webapp",
                   "url": "https://127.0.0.1:9999/"}]}
_SKILLS = {"skills": [{"id": "demo-skill", "name": "demo-skill", "is_favorite": False}]}


def _recorded(page: Page, pattern: str, body: dict) -> list:
    """Serve `body` and record each call: a read that re-renders the list."""
    calls: list = []

    def _handle(route):
        calls.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body=_json.dumps(body))
    page.route(re.compile(pattern), _handle)
    return calls


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
    # The git-status refresh re-renders the Apps list (renderApps) on its own.
    git = _recorded(page, r".*/api/claude-code/git-status$", {"projects": []})
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabApps").click()

    state = page.locator("#appsState")
    expect(state).to_have_attribute("data-state", "error")
    expect(state.locator(".empty-state-message")).to_have_text("Could not load the apps.")
    # A failed read is not an empty registry: the true-empty block stays out,
    # also once another read has re-rendered the list.
    wait_until(page, lambda: len(git) > 0, "the git-status read")
    flush_requests(page)
    expect(page.locator("#appsEmpty")).to_be_hidden()
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
    channels = _recorded(page, r".*/api/life-os/channels$", {"profiles": [], "problems": []})
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabLifeOS").click()

    state = page.locator("#lifeOsState")
    expect(state).to_have_attribute("data-state", "error")
    expect(state.locator(".empty-state-message")).to_have_text("Could not load the skills.")
    # Not "no skills found" as well: the channels read that follows every
    # skills read re-renders the list, and a failed read is not an empty folder.
    wait_until(page, lambda: len(channels) > 0, "the channels read")
    flush_requests(page)
    expect(page.locator("#lifeOsEmpty")).to_be_hidden()
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
