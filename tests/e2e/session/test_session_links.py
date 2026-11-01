"""A session's ⋮ → Links.

Every artifact and URL a session's transcript mentioned, folded server-side
and grouped. Pinned against stubbed routes (every name and URL synthetic):

* the item is in the session view's ⋮ menu and opens a sheet listing each
  group's rows once, a row being a link that opens in a new tab;
* a session with no links says so;
* rows meet the 44px floor and nothing scrolls sideways at 390px.
"""
from __future__ import annotations

import json as _json
import re

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


def _link(url: str, label: str, count: int = 1) -> dict:
    return {"url": url, "label": label, "first_seen": "2026-10-01T10:00:00Z",
            "last_seen": "2026-10-01T10:05:00Z", "count": count}


_LINKS = {
    "available": True, "reason": None, "session_id": _SID, "total": 3, "partial": False,
    "groups": [
        {"key": "artifacts", "title": "Artifacts", "total": 1,
         "links": [_link("https://claude.ai/artifact/AbCdEfGh123456", "Weekly Report")]},
        {"key": "github", "title": "GitHub", "total": 1,
         "links": [_link("https://github.com/acme/widgets/pull/12", "acme/widgets#12", 2)]},
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
  if (!rows.length || !rows[0].getBoundingClientRect().height) return null;
  const de = document.documentElement;
  const body = document.getElementById('linksBody');
  return {minRow: Math.min(...rows.map(x => x.getBoundingClientRect().height)),
          minCopy: Math.min(...copies.map(x => x.getBoundingClientRect().height)),
          body: body.scrollWidth - body.clientWidth, doc: de.scrollWidth - de.clientWidth};
})()
"""


def test_links_lists_each_group_and_opens_in_a_new_tab(authed_page: Page, base_url: str) -> None:
    _mock(authed_page, _LINKS)
    _open_links(authed_page, base_url)

    sheet = authed_page.locator("#linksSheet")
    expect(sheet).to_be_visible()
    expect(sheet.locator(".links-group-title")).to_have_text(
        ["Artifacts 1", "GitHub 1", "Servers & previews 1"])
    rows = sheet.locator(".link-open")
    expect(rows).to_have_count(3)
    expect(rows.first.locator(".link-title")).to_have_text("Weekly Report")
    expect(rows.first).to_have_attribute("href", "https://claude.ai/artifact/AbCdEfGh123456")
    expect(rows.first).to_have_attribute("target", "_blank")
    expect(sheet.locator(".link-copy")).to_have_count(3)

    fit = stable_read(lambda: authed_page.evaluate(_FIT))
    assert fit["minRow"] >= 44 and fit["minCopy"] >= 44, f"a row is under the 44px floor: {fit}"
    assert fit["body"] <= 0 and fit["doc"] <= 0, f"sideways scroll at 390px: {fit}"

    authed_page.locator("#linksDone").click()
    expect(sheet).to_be_hidden()
    expect(authed_page.locator("#terminalOverlay")).to_be_visible()


def test_links_empty_session_says_so(authed_page: Page, base_url: str) -> None:
    _mock(authed_page, _EMPTY)
    _open_links(authed_page, base_url)
    expect(authed_page.locator("#linksState")).to_contain_text("No links in this session")
    expect(authed_page.locator("#linksSheet .link-open")).to_have_count(0)
