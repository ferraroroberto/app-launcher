"""A session's ⋮ → Links.

Every artifact and URL a session's transcript mentioned, folded server-side
and grouped. Pinned against stubbed routes (every name and URL synthetic):

* the item is in the session view's ⋮ menu and opens a sheet of folded groups
  (a name and a count); a tap unfolds one into its rows, newest first, each a
  link that opens in a new tab under its real title with a time label;
* a session with no links says so;
* rows meet the 44px floor and nothing scrolls sideways at 390px.
"""
from __future__ import annotations

import json as _json
import re
from datetime import datetime, timedelta, timezone

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.waits import OVERLAY_OPEN_MS, stable_read
from tests.e2e.session.test_session_mode_toggle import (
    _mock_git_status,
    _mock_sessions_list,
    _row,
    _session_row,
)

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_SID = "sid-links-2043"
_TRANSCRIPT = {
    "available": True, "source": "native", "reason": None, "session_id": _SID,
    "next_cursor": None, "tool_errors": "reported",
    "entries": [
        {"kind": "user", "timestamp": "2026-10-01T10:00:00Z", "text": "share the links",
         "truncated": False, "sidechain": False},
    ],
}


def _stamp(minutes_ago: int) -> str:
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _link(url: str, label: str, count: int = 1, title=None, minutes_ago: int = 5) -> dict:
    return {"url": url, "label": label, "title": title, "first_seen": _stamp(minutes_ago + 5),
            "last_seen": _stamp(minutes_ago), "count": count}


_LINKS = {
    "available": True, "reason": None, "session_id": _SID, "total": 4, "partial": False,
    "groups": [
        {"key": "artifacts", "title": "Artifacts", "total": 2,
         "links": [_link("https://claude.ai/artifact/AbCdEfGh123456", "Artifact AbCdEfGh",
                         title="Weekly Report", minutes_ago=12),
                   _link("https://claude.ai/artifact/ZzZzZzZz987654", "Artifact ZzZzZzZz",
                         minutes_ago=300)]},
        {"key": "github", "title": "GitHub", "total": 1,
         "links": [_link("https://github.com/acme/widgets/pull/12", "acme/widgets#12", 2,
                         title="acme/widgets#12 · Fix the widget")]},
        {"key": "servers", "title": "Servers & previews", "total": 1,
         "links": [_link("http://100.64.1.2:8123/index.html", "100.64.1.2:8123")]},
    ],
}
_EMPTY = {"available": True, "reason": None, "session_id": _SID,
          "groups": [], "total": 0, "partial": False}


def _mock(page: Page, links: dict) -> None:
    _mock_git_status(page)
    _mock_sessions_list(page, [_session_row(_SID, kind="remote", agent="claude", title="Links demo")])
    page.route(
        re.compile(r".*/api/claude-code/sessions/" + re.escape(_SID) + r"/transcript(\?.*)?$"),
        lambda route: route.fulfill(status=200, content_type="application/json",
                                    body=_json.dumps(_TRANSCRIPT)),
    )
    page.route(
        re.compile(r".*/api/claude-code/sessions/" + re.escape(_SID) + r"/links(\?.*)?$"),
        lambda route: route.fulfill(status=200, content_type="application/json",
                                    body=_json.dumps(links)),
    )


def _open_links(page: Page, base_url: str) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(base_url, wait_until="domcontentloaded")
    _row(page, _SID).locator(".session-open").click()
    expect(page.locator("#terminalOverlay")).to_be_visible(timeout=OVERLAY_OPEN_MS)
    page.locator("#terminalMenu").click()
    page.locator(".terminal-menu").get_by_role("menuitem", name="Links").click()


_FIT = """
(() => {
  const sheet = document.getElementById('linksSheet');
  const rows = [...sheet.querySelectorAll('.link-open')];
  const copies = [...sheet.querySelectorAll('.link-copy')];
  const heads = [...sheet.querySelectorAll('.links-group-head')];
  if (!rows.length || !rows[0].getBoundingClientRect().height) return null;
  const de = document.documentElement;
  const body = document.getElementById('linksBody');
  return {minRow: Math.min(...rows.map(x => x.getBoundingClientRect().height)),
          minCopy: Math.min(...copies.map(x => x.getBoundingClientRect().height)),
          minHead: Math.min(...heads.map(x => x.getBoundingClientRect().height)),
          body: body.scrollWidth - body.clientWidth, doc: de.scrollWidth - de.clientWidth};
})()
"""


def test_links_groups_open_folded_and_unfold_into_titled_rows(authed_page: Page, base_url: str) -> None:
    _mock(authed_page, _LINKS)
    _open_links(authed_page, base_url)

    sheet = authed_page.locator("#linksSheet")
    expect(sheet).to_be_visible()
    groups = sheet.locator(".links-group")
    expect(groups.locator(".links-group-title")).to_have_text(["Artifacts", "GitHub", "Servers & previews"])
    expect(groups.locator(".links-group-count")).to_have_text(["2", "1", "1"])
    # Every group opens folded: no row is on screen until its head is tapped.
    for index in range(3):
        expect(groups.nth(index)).not_to_have_attribute("open", re.compile(".*"))
    expect(sheet.locator(".link-open").first).to_be_hidden()
    # The fold toggle is a constant tracking id from the markup.
    expect(sheet.locator(".links-group-head").first).to_have_attribute("data-track", "links.group-fold")

    for index in range(3):
        groups.nth(index).locator(".links-group-head").click()
    rows = sheet.locator(".link-open")
    expect(rows).to_have_count(4)
    # A real title, not "Artifact <id>"; the label stands in when none was found.
    expect(rows.nth(0).locator(".link-title")).to_have_text("Weekly Report")
    expect(rows.nth(1).locator(".link-title")).to_have_text("Artifact ZzZzZzZz")
    expect(rows.nth(2).locator(".link-title")).to_have_text("acme/widgets#12 · Fix the widget")
    # Newest first, each with when it was last mentioned.
    expect(rows.nth(0).locator(".link-meta")).to_contain_text(re.compile(r"^\d+ min ago"))
    expect(rows.nth(1).locator(".link-meta")).to_contain_text(re.compile(r"^\d+ h ago"))
    expect(rows.first).to_have_attribute("href", "https://claude.ai/artifact/AbCdEfGh123456")
    expect(rows.first).to_have_attribute("target", "_blank")
    expect(sheet.locator(".link-copy")).to_have_count(4)

    # A chevron mid-turn reaches past the edge: measure once the folds have settled.
    authed_page.wait_for_function("document.getAnimations().length === 0")
    fit = stable_read(lambda: authed_page.evaluate(_FIT))
    assert fit["minRow"] >= 44 and fit["minCopy"] >= 44, f"a row is under the 44px floor: {fit}"
    assert fit["minHead"] >= 52, f"a group head is under the 52px row: {fit}"
    assert fit["body"] <= 0 and fit["doc"] <= 0, f"sideways scroll at 390px: {fit}"

    groups.nth(0).locator(".links-group-head").click()
    expect(rows.first).to_be_hidden()

    authed_page.locator("#linksDone").click()
    expect(sheet).to_be_hidden()
    expect(authed_page.locator("#terminalOverlay")).to_be_visible()


def test_links_empty_session_says_so(authed_page: Page, base_url: str) -> None:
    _mock(authed_page, _EMPTY)
    _open_links(authed_page, base_url)
    expect(authed_page.locator("#linksState")).to_contain_text("No links in this session")
    expect(authed_page.locator("#linksSheet .link-open")).to_have_count(0)


def test_links_partial_read_names_its_cut_off_and_read_more_merges_the_older_window(
    authed_page: Page, base_url: str,
) -> None:
    newest = {**_LINKS, "total": 1, "partial": True, "offset": 4096, "read_from": "2026-10-31T12:28:00Z",
              "groups": [{"key": "artifacts", "title": "Artifacts", "total": 1, "links": [
                  _link("https://claude.ai/artifact/AbCdEfGh123456", "Artifact AbCdEfGh")]}]}
    older = {**_LINKS, "total": 2, "partial": False, "offset": 0, "read_from": "2026-10-01T09:00:00Z",
             "groups": [{"key": "artifacts", "title": "Artifacts", "total": 2, "links": [
                 _link("https://claude.ai/artifact/AbCdEfGh123456", "Weekly Report", title="Weekly Report",
                       minutes_ago=900),
                 _link("https://claude.ai/artifact/ZzZzZzZz987654", "Artifact ZzZzZzZz", minutes_ago=950)]}]}
    asked: list = []

    def answer(route) -> None:
        asked.append(route.request.url)
        body = older if "before=4096" in route.request.url else newest
        route.fulfill(status=200, content_type="application/json", body=_json.dumps(body))

    _mock(authed_page, newest)
    authed_page.route(re.compile(r".*/api/claude-code/sessions/" + re.escape(_SID) + r"/links(\?.*)?$"), answer)
    _open_links(authed_page, base_url)

    sheet = authed_page.locator("#linksSheet")
    note = sheet.locator(".links-read .links-note")
    expect(note).to_contain_text(re.compile(r"^Read back to .*\d"))
    more = sheet.get_by_role("button", name="Read more")
    expect(more).to_have_attribute("data-track", "links.read-more")
    sheet.locator(".links-group-head").click()
    expect(sheet.locator(".link-open")).to_have_count(1)

    more.click()
    expect(note).to_have_text("Whole session read")
    expect(more).to_have_count(0)
    # The row both windows mention is one row, now named; the group stays unfolded.
    rows = sheet.locator(".link-open")
    expect(rows).to_have_count(2)
    expect(rows.nth(0).locator(".link-title")).to_have_text("Weekly Report")
    expect(sheet.locator(".links-group-count")).to_have_text("2")
    assert any(url.endswith("before=4096") for url in asked), asked
