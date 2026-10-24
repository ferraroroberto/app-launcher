"""Life OS tab e2e: finding, reading and restoring what the zombie cleanup archived."""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.life_os._support import (
    _ARCHIVED_URL,
    _FAKE_CONVERSATIONS,
    _convo_rows,
    _mock_search,
    _mock_skills,
    _open_life,
)


pytestmark = pytest.mark.smoke

_ENTRY = {
    "day": "2026-09-14", "skill": "journal-daily",
    "capture": "2026-09-13-0800-ferry-plans.md", "title": "",
    "agent": "claude", "moved_at": "2026-09-14T04:00:00+02:00",
}
_ENTRY_URL = "/api/life-os/archived/2026-09-14/journal-daily/2026-09-13-0800-ferry-plans.md"
_TRANSCRIPT = {
    "name": _ENTRY["capture"], "agent": "claude", "available": True, "truncated": False,
    "entries": [{"kind": "assistant", "text": "Hello, shall we plan the island ferry?",
                 "offset": 0, "timestamp": None, "truncated": False}],
}


def _mock_archive(page: Page, restore_status: int, restore_body: dict) -> list:
    """The backup holds one entry until a restore answers 200; returns the restore calls."""
    restores: list = []

    def _list(route):
        rows = [] if restores and restore_status == 200 else [_ENTRY]
        route.fulfill(status=200, content_type="application/json",
                      body=_json.dumps({"archived": rows}))

    def _restore(route):
        restores.append(route.request.method)
        route.fulfill(status=restore_status, content_type="application/json",
                      body=_json.dumps(restore_body))

    page.route(_ARCHIVED_URL, _list)
    page.route(re.compile(re.escape(_ENTRY_URL) + "/transcript$"), lambda route: route.fulfill(
        status=200, content_type="application/json", body=_json.dumps(_TRANSCRIPT)))
    page.route(re.compile(re.escape(_ENTRY_URL) + "/restore$"), _restore)
    return restores


def _search_and_open_archived(page: Page, base_url: str) -> None:
    _mock_skills(page)
    _mock_search(page, [dict(_FAKE_CONVERSATIONS["conversations"][0])])
    _open_life(page, base_url)
    archived = page.locator("#lifeOsArchived")
    expect(archived).to_be_hidden()     # nothing typed: nothing archived shows
    page.locator("#lifeOsFilterInput").fill("ferry")
    expect(_convo_rows(page)).to_have_count(1, timeout=5_000)
    expect(page.locator("#lifeOsConvoHead")).to_have_text("Conversations (1)")
    # Apart from the live list, counted, and collapsed until asked for.
    expect(archived).to_be_visible()
    expect(page.locator("#lifeOsArchivedCount")).to_have_text("(1)")
    rows = page.locator("#lifeOsArchivedList .lifeos-archived-row")
    expect(rows.first).to_be_hidden()
    page.locator(".lifeos-archived-summary").click()
    expect(rows).to_have_count(1)
    expect(rows.first.locator(".action-row-title")).to_have_text("ferry plans")
    expect(rows.first.locator(".action-row-meta")).to_contain_text("journal-daily")
    rows.first.locator(".action-row-main").click()
    viewer = page.locator("#lifeOsConvoViewer")
    expect(viewer).to_be_visible(timeout=5_000)
    expect(page.locator("#lifeOsViewerList")).to_contain_text("plan the island ferry")
    expect(page.locator("#lifeOsViewerDone")).to_have_text("Restore")


def test_an_archived_search_hit_reads_and_restores(authed_page: Page, base_url: str) -> None:
    restores = _mock_archive(authed_page, 200, {"status": "restored"})
    _search_and_open_archived(authed_page, base_url)
    authed_page.locator("#lifeOsViewerDone").click()
    expect(authed_page.locator("#lifeOsConvoViewer")).to_be_hidden(timeout=5_000)
    expect(authed_page.locator("#toast")).to_contain_text("Restored ferry plans")
    assert restores == ["POST"], restores
    # The refresh after the restore finds the backup empty: the group goes.
    expect(authed_page.locator("#lifeOsArchived")).to_be_hidden(timeout=5_000)
    # Clearing the search hides the whole group.
    authed_page.locator("#lifeOsFilterInput").fill("")
    expect(authed_page.locator("#lifeOsConvoGroup")).to_be_hidden(timeout=5_000)


def test_a_refused_restore_says_why_and_stays_open(authed_page: Page, base_url: str) -> None:
    _mock_archive(authed_page, 409, {"detail": "a capture with this name already exists"})
    _search_and_open_archived(authed_page, base_url)
    done = authed_page.locator("#lifeOsViewerDone")
    done.click()
    expect(authed_page.locator("#toast")).to_contain_text("a capture with this name already exists")
    expect(authed_page.locator("#lifeOsConvoViewer")).to_be_visible()
    expect(done).to_have_text("Restore")
    expect(done).to_be_enabled()
    expect(authed_page.locator("#lifeOsArchivedCount")).to_have_text("(1)")
