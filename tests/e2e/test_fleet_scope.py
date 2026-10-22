"""Settings > Fleet scope: one switch per repo, on = active.

Hermetic: both fleet routes are mocked, so no test reads or writes a real
fleet-config checkout. Covered: the list and its count line, a switch saving at
once with a toast, a refused save putting the switch back with the writer's own
message, the contract-unavailable state (a message, no switches), and the
filter that appears above twelve rows.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import open_settings_sheet

pytestmark = pytest.mark.smoke


def _body(repos: dict[str, str], **extra) -> str:
    return json.dumps({
        "available": True, "reason": "",
        "repos": [{"name": n, "status": s} for n, s in sorted(repos.items())],
        **extra,
    })


def _mock(page: Page, repos: dict[str, str], *, refuse: str = "", unavailable: str = "") -> list:
    """Route-mock the two fleet routes; returns the PUT bodies seen."""
    puts: list = []

    def _get(route):
        if unavailable:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(
                {"available": False, "reason": unavailable, "repos": []}))
        else:
            route.fulfill(status=200, content_type="application/json", body=_body(repos))

    def _put(route):
        name = route.request.url.rsplit("/", 1)[1]
        puts.append((name, route.request.post_data_json))
        if refuse:
            route.fulfill(status=409, content_type="application/json",
                          body=json.dumps({"detail": refuse}))
            return
        repos[name] = route.request.post_data_json["status"]
        route.fulfill(status=200, content_type="application/json",
                      body=_body(repos, outcome="changed"))

    page.route(re.compile(r".*/api/fleet/scope$"), _get)
    page.route(re.compile(r".*/api/fleet/scope/[^/]+$"), _put)
    return puts


def _open(page: Page, base_url: str) -> None:
    page.goto(base_url, wait_until="domcontentloaded")
    open_settings_sheet(page, "fleetScopeSheet")


def _switch(page: Page, repo: str):
    return page.locator("#fleetScopeList .action-row", has_text=repo).locator("[role=switch]")


def test_lists_repos_with_a_count_and_the_row_value(authed_page: Page, base_url: str) -> None:
    _mock(authed_page, {"alpha": "active", "beta": "dormant", "gamma": "active"})
    _open(authed_page, base_url)
    expect(authed_page.locator("#fleetScopeCount")).to_have_text("2 active, 1 dormant")
    expect(authed_page.locator("#fleetScopeList .action-row")).to_have_count(3)
    expect(_switch(authed_page, "alpha")).to_have_attribute("aria-checked", "true")
    expect(_switch(authed_page, "beta")).to_have_attribute("aria-checked", "false")
    expect(authed_page.locator("#fleetScopeUnavailable")).to_be_hidden()
    expect(authed_page.locator("#fleetScopeFilterWrap")).to_be_hidden()
    authed_page.locator("#fleetScopeSheet [data-sheet-close]").last.click()
    expect(authed_page.locator('[data-settings-value="fleet-scope"]')).to_have_text("2 active, 1 dormant")


def test_a_switch_saves_at_once_and_toasts(authed_page: Page, base_url: str) -> None:
    puts = _mock(authed_page, {"alpha": "active", "beta": "dormant"})
    _open(authed_page, base_url)
    _switch(authed_page, "alpha").click()
    expect(authed_page.locator("#toast")).to_contain_text("alpha is dormant")
    expect(authed_page.locator("#fleetScopeCount")).to_have_text("0 active, 2 dormant")
    expect(_switch(authed_page, "alpha")).to_have_attribute("aria-checked", "false")
    assert puts == [("alpha", {"status": "dormant"})]


def test_a_refused_save_puts_the_switch_back_with_the_real_message(authed_page: Page, base_url: str) -> None:
    _mock(authed_page, {"alpha": "active"}, refuse="array is not editable")
    _open(authed_page, base_url)
    _switch(authed_page, "alpha").click()
    expect(authed_page.locator("#toast")).to_contain_text("array is not editable")
    expect(_switch(authed_page, "alpha")).to_have_attribute("aria-checked", "true")
    expect(_switch(authed_page, "alpha")).to_be_enabled()


def test_unavailable_contract_says_why_and_shows_no_switches(authed_page: Page, base_url: str) -> None:
    _mock(authed_page, {}, unavailable="This fleet-config checkout has no fleet scope support yet.")
    _open(authed_page, base_url)
    expect(authed_page.locator("#fleetScopeReason")).to_have_text(
        "This fleet-config checkout has no fleet scope support yet.")
    expect(authed_page.locator("#fleetScopeList [role=switch]")).to_have_count(0)
    expect(authed_page.locator("#fleetScopeCount")).to_be_hidden()
    authed_page.locator("#fleetScopeSheet [data-sheet-close]").last.click()
    expect(authed_page.locator('[data-settings-value="fleet-scope"]')).to_have_text("Unavailable")


def test_a_long_list_gets_a_filter(authed_page: Page, base_url: str) -> None:
    _mock(authed_page, {f"repo-{i:02d}": "active" for i in range(13)})
    _open(authed_page, base_url)
    expect(authed_page.locator("#fleetScopeFilterWrap")).to_be_visible()
    authed_page.locator("#fleetScopeFilterInput").fill("repo-07")
    expect(authed_page.locator("#fleetScopeList .action-row:not([hidden])")).to_have_count(1)
    authed_page.locator("#fleetScopeFilterInput").fill("nothing-like-this")
    expect(authed_page.locator("#fleetScopeFilterEmpty")).to_be_visible()
