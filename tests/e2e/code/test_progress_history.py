"""The Usage card's ⋮ menu and the History sheet it opens.

The kebab holds two items: Usage details (what a tap on the card has always
opened) and History, the progress dashboard. The sheet answers from
``/api/progress``'s cache at once, however old, says how old it is, shows a
toast while the on-demand refresh runs, and updates in place when it ends. A
figure that was not measured shows its word (unknown, unavailable), never zero.

Hermetic: ``/api/progress``, the sessions list and the quota are route-mocked
before ``goto()`` with invented numbers, so nothing reads GitHub or the hub.
The refresh is driven with the fake page clock, not by waiting out a poll.
"""

from __future__ import annotations

import json as _json
import re
from typing import Any, Dict, List, Optional

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import _fake_clock
from tests.e2e.fixtures.ui import open_row_menu
from tests.e2e.fixtures.waits import wait_until

pytestmark = pytest.mark.smoke


def _day(start: str, github: Optional[Dict[str, int]], tokens: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {"start": start, "end": start, "github": github, "tokens": tokens}


def _github(added: int, removed: int, prs: int, opened: int, closed: int) -> Dict[str, int]:
    return {"lines_added": added, "lines_removed": removed, "prs_merged": prs,
            "issues_opened": opened, "issues_closed": closed}


def _measured(tin: int, tout: int) -> Dict[str, Any]:
    return {"state": "measured", "tokens_in": tin, "tokens_out": tout, "unknown_days": 0}


_NOT_MEASURED = {"state": "not_measured", "tokens_in": None, "tokens_out": None, "unknown_days": 0}


def _payload(*, added: int, refreshing: bool = False, tokens_state: str = "ok", age: float = 4000.0) -> Dict[str, Any]:
    tokens_ok = tokens_state == "ok"
    return {
        "bucket": "day", "from": "2026-10-06", "to": "2026-11-04",
        "generated_at": "2026-11-04T10:00:00+00:00", "day_basis": {"github": "local", "tokens": "utc"},
        "refreshing": refreshing,
        "github": {"state": "ok", "reason": None, "refreshed_at": "2026-11-04T08:53:00+00:00",
                   "age_seconds": age, "refreshing": refreshing, "unknown_repos": [], "skipped": []},
        "tokens": {"state": tokens_state, "reason": None, "computed_at": None,
                   "age_seconds": age if tokens_ok else None, "refreshing": False},
        "buckets": [
            _day("2026-11-02", _github(added, 40, 2, 3, 1),
                 {"claude": _measured(1000, 200), "grok": _NOT_MEASURED} if tokens_ok else None),
            _day("2026-11-03", _github(50, 10, 1, 0, 2),
                 {"claude": _measured(500, 50), "grok": _NOT_MEASURED} if tokens_ok else None),
        ],
        "repos": [
            {"repo": "atlas-web", "state": "ok", **_github(added, 40, 2, 3, 1)},
            {"repo": "beacon-api", "state": "ok", **_github(50, 10, 1, 0, 2)},
        ],
    }


class _Progress:
    """Stands in for ``GET /api/progress``: the cache, a refresh that runs until released."""

    def __init__(self, cached: Dict[str, Any], refreshed: Dict[str, Any]) -> None:
        self.cached = cached
        self.refreshed = refreshed
        self.released = False
        self.requests: List[str] = []

    def __call__(self, route) -> None:
        url = route.request.url
        self.requests.append(url)
        if self.released:
            body = self.refreshed
        elif "refresh=true" in url:
            body = dict(self.cached, refreshing=True)
        else:
            body = dict(self.cached, refreshing=self.cached.get("refreshing", False) or self._running())
        route.fulfill(status=200, content_type="application/json", body=_json.dumps(body))

    def _running(self) -> bool:
        return any("refresh=true" in u for u in self.requests)


def _boot(page: Page, base_url: str, progress: _Progress) -> None:
    def json_route(pattern: str, body: Dict[str, Any]) -> None:
        page.route(re.compile(pattern), lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(body)))
    json_route(r".*/api/claude-code/sessions$", {"sessions": []})
    json_route(r".*/api/claude-code/git-status$", {"projects": []})
    json_route(r".*/api/rate-limits$", {"quota_lines": []})
    page.route(re.compile(r".*/api/progress(\?.*)?$"), progress)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")


def _open_history(page: Page) -> None:
    page.locator("#codingUsageKebab").click()
    open_row_menu(page).get_by_role("menuitem", name="History").click()
    expect(page.locator("#progressSheet")).to_be_visible()


def _tile(page: Page, key: str):
    return page.locator(f'#progressTiles .progress-tile[data-tile="{key}"]')


def test_kebab_menu_keeps_usage_details_and_adds_history(authed_page: Page, base_url: str) -> None:
    page = authed_page
    _boot(page, base_url, _Progress(_payload(added=100), _payload(added=250)))

    # The kebab opens the menu and does not open the Usage sheet under it.
    page.locator("#codingUsageKebab").click()
    menu = open_row_menu(page)
    expect(menu.get_by_role("menuitem")).to_have_text(["Usage details", "History"])
    expect(page.locator("#usageSheet")).not_to_be_visible()

    # Usage details is the sheet a tap on the card has always opened.
    menu.get_by_role("menuitem", name="Usage details").click()
    expect(page.locator("#usageSheet")).to_be_visible()
    page.locator("#usageSheetClose").click()

    # A tap on the card itself still opens it.
    page.locator("#codingUsageTitle").click()
    expect(page.locator("#usageSheet")).to_be_visible()
    page.locator("#usageSheetClose").click()

    _open_history(page)
    expect(page.locator("#progressSheetTitle")).to_have_text("History")
    page.locator("#progressSheetClose").click()
    expect(page.locator("#progressSheet")).not_to_be_visible()


def test_cached_figures_show_at_once_then_update_in_place(authed_page: Page, base_url: str) -> None:
    page = authed_page
    _fake_clock.install(page)
    progress = _Progress(_payload(added=100), _payload(added=250, age=5.0))
    _boot(page, base_url, progress)

    _open_history(page)
    # The cached figures, however old, with their age; the refresh toast rides along.
    expect(_tile(page, "lines")).to_contain_text("+150")
    toast = page.locator("#progressToast")
    expect(toast).to_be_visible()
    expect(toast).to_contain_text("Refreshing")
    expect(page.locator('#progressSources li[data-source="github"]')).to_contain_text("1 h ago")
    expect(page.locator("#progressRefresh")).to_be_disabled()

    # The refresh ends: the sheet updates in place, the toast goes.
    progress.released = True

    def next_poll_landed() -> bool:
        _fake_clock.advance(page, 4000)
        return "+300" in _tile(page, "lines").inner_text()

    wait_until(page, next_poll_landed, "the sheet to read the finished refresh")
    expect(toast).to_be_hidden()
    expect(page.locator("#progressRefresh")).to_be_enabled()
    expect(page.locator('#progressSources li[data-source="github"]')).to_contain_text("just now")

    # Opening asked for exactly one recalculation.
    asks = [u for u in progress.requests if "refresh=true" in u]
    assert len(asks) == 1, asks


def test_bucket_and_range_reread_the_cache_only(authed_page: Page, base_url: str) -> None:
    page = authed_page
    progress = _Progress(_payload(added=100, refreshing=False), _payload(added=100))
    progress.released = True
    _boot(page, base_url, progress)
    _open_history(page)
    wait_until(page, lambda: any("refresh=true" in u for u in progress.requests), "the opening refresh")

    before = len(progress.requests)
    page.locator('#progressSheet [data-bucket="week"]').click()
    wait_until(page, lambda: len(progress.requests) > before, "the week read")
    assert "bucket=week" in progress.requests[-1]
    assert "refresh=true" not in progress.requests[-1]

    page.locator('#progressSheet [data-range="90"]').click()
    wait_until(page, lambda: "bucket=week" in progress.requests[-1] and len(progress.requests) > before + 1, "the 90d read")
    expect(page.locator('#progressSheet [data-range="90"]')).to_have_attribute("aria-pressed", "true")


def test_unmeasured_figures_say_so_and_never_read_zero(authed_page: Page, base_url: str) -> None:
    page = authed_page
    down = _payload(added=100, tokens_state="unavailable")
    progress = _Progress(down, down)
    progress.released = True
    _boot(page, base_url, progress)
    _open_history(page)

    tokens = _tile(page, "tokens")
    expect(tokens).to_contain_text("unavailable")
    expect(tokens).not_to_contain_text("0")
    # The GitHub half still renders beside it.
    expect(_tile(page, "lines")).to_contain_text("+150")
    expect(page.locator('#progressSources li[data-source="tokens"]')).to_contain_text("unavailable")
    # Grok's logs carry no counts: a word, not a share.
    unmeasured = _payload(added=100)
    progress.cached = progress.refreshed = unmeasured
    page.locator('#progressSheet [data-bucket="week"]').click()
    expect(page.locator('#progressAgents .progress-agent-row[data-agent="grok"]')).to_contain_text("Not measured")


@pytest.mark.iphone
def test_sheet_fits_a_phone(authed_page: Page, base_url: str) -> None:
    page = authed_page
    page.set_viewport_size({"width": 390, "height": 844})
    progress = _Progress(_payload(added=100), _payload(added=100))
    progress.released = True
    _boot(page, base_url, progress)
    _open_history(page)
    expect(_tile(page, "lines")).to_contain_text("+150")
    expect(page.locator("#progressChartLoc svg")).to_be_visible()

    dialog = page.locator("#progressSheet")
    fits = dialog.evaluate("""d => {
      const box = d.getBoundingClientRect();
      const wide = Array.from(d.querySelectorAll('.progress-body *')).filter(
        e => e.getBoundingClientRect().right > box.right + 1 && !e.closest('.progress-table-wrap'));
      return { overflowX: d.scrollWidth - d.clientWidth, width: box.width, wide: wide.map(e => e.className.toString()) };
    }""")
    assert fits["overflowX"] <= 1, fits
    assert fits["wide"] == [], fits
    done = page.locator("#progressSheetDone").bounding_box()
    assert done is not None and done["height"] >= 44, done
