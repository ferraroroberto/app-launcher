"""Settings > Fleet scope: one switch per repo, on = active.

Hermetic: both fleet routes are mocked, so no test reads or writes a real
fleet-config checkout. Covered: the list and its count line, a switch saving at
once with a toast, a refused save putting the switch back with the writer's own
message, the filter that appears above twelve rows, and the contract-unavailable
state (a message, no switches). The sheet reads the scope each time it opens,
so one page walks every state by changing the mock and reopening it.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.ui import open_settings_sheet

pytestmark = pytest.mark.smoke


def _body(repos: dict[str, str], **extra) -> str:
    return json.dumps({
        "available": True, "reason": "",
        "repos": [{"name": n, "status": s} for n, s in sorted(repos.items())],
        **extra,
    })


def _mock(page: Page, state: dict) -> list:
    """Route-mock the two fleet routes from ``state`` (``repos``, ``refuse``,
    ``unavailable``), read on every request; returns the PUT bodies seen."""
    puts: list = []

    def _get(route):
        if state["unavailable"]:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(
                {"available": False, "reason": state["unavailable"], "repos": []}))
        else:
            route.fulfill(status=200, content_type="application/json", body=_body(state["repos"]))

    def _put(route):
        name = route.request.url.rsplit("/", 1)[1]
        puts.append((name, route.request.post_data_json))
        if state["refuse"]:
            route.fulfill(status=409, content_type="application/json",
                          body=json.dumps({"detail": state["refuse"]}))
            return
        state["repos"][name] = route.request.post_data_json["status"]
        route.fulfill(status=200, content_type="application/json",
                      body=_body(state["repos"], outcome="changed"))

    page.route(re.compile(r".*/api/fleet/scope$"), _get)
    page.route(re.compile(r".*/api/fleet/scope/[^/]+$"), _put)
    return puts


def _close(page: Page) -> None:
    page.locator("#fleetScopeSheet [data-sheet-close]").last.click()
    expect(page.locator("#fleetScopeSheet")).to_be_hidden()


def _switch(page: Page, repo: str):
    return page.locator("#fleetScopeList .action-row", has_text=repo).locator("[role=switch]")


def test_fleet_scope_lists_saves_refuses_filters_and_says_when_unavailable(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    state: dict = {"repos": {"alpha": "active", "beta": "dormant", "gamma": "active"},
                   "refuse": "", "unavailable": ""}
    puts = _mock(page, state)
    page.goto(base_url, wait_until="domcontentloaded")
    open_settings_sheet(page, "fleetScopeSheet")

    # The list, its count line and the row's value.
    expect(page.locator("#fleetScopeCount")).to_have_text("2 active, 1 dormant")
    expect(page.locator("#fleetScopeList .action-row")).to_have_count(3)
    expect(_switch(page, "alpha")).to_have_attribute("aria-checked", "true")
    expect(_switch(page, "beta")).to_have_attribute("aria-checked", "false")
    expect(page.locator("#fleetScopeUnavailable")).to_be_hidden()
    expect(page.locator("#fleetScopeFilterWrap")).to_be_hidden()
    _close(page)
    expect(page.locator('[data-settings-value="fleet-scope"]')).to_have_text("2 active, 1 dormant")

    # A switch saves at once and toasts.
    open_settings_sheet(page, "fleetScopeSheet")
    _switch(page, "alpha").click()
    expect(page.locator("#toast")).to_contain_text("alpha is dormant")
    expect(page.locator("#fleetScopeCount")).to_have_text("1 active, 2 dormant")
    expect(_switch(page, "alpha")).to_have_attribute("aria-checked", "false")
    assert puts == [("alpha", {"status": "dormant"})]

    # A refused save puts the switch back, with the writer's own message.
    state["refuse"] = "array is not editable"
    _switch(page, "gamma").click()
    expect(page.locator("#toast")).to_contain_text("array is not editable")
    expect(_switch(page, "gamma")).to_have_attribute("aria-checked", "true")
    expect(_switch(page, "gamma")).to_be_enabled()
    _close(page)

    # A long list gets a filter.
    state.update(refuse="", repos={f"repo-{i:02d}": "active" for i in range(13)})
    open_settings_sheet(page, "fleetScopeSheet")
    expect(page.locator("#fleetScopeFilterWrap")).to_be_visible()
    page.locator("#fleetScopeFilterInput").fill("repo-07")
    expect(page.locator("#fleetScopeList .action-row:not([hidden])")).to_have_count(1)
    page.locator("#fleetScopeFilterInput").fill("nothing-like-this")
    expect(page.locator("#fleetScopeFilterEmpty")).to_be_visible()
    _close(page)

    # The contract unavailable: why, and no switches.
    state["unavailable"] = "This fleet-config checkout has no fleet scope support yet."
    open_settings_sheet(page, "fleetScopeSheet")
    expect(page.locator("#fleetScopeReason")).to_have_text(
        "This fleet-config checkout has no fleet scope support yet.")
    expect(page.locator("#fleetScopeList [role=switch]")).to_have_count(0)
    expect(page.locator("#fleetScopeCount")).to_be_hidden()
    _close(page)
    expect(page.locator('[data-settings-value="fleet-scope"]')).to_have_text("Unavailable")
