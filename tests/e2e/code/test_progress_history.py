"""The Usage card's ⋮ menu and the History sheet it opens.

The kebab holds two items: Usage details (what a tap on the card has always
opened) and History, the progress dashboard. The sheet answers from
``/api/progress``'s cache at once, however old, says when it was last
refreshed and never refreshes on open. A tap on Refresh starts one: a neutral
toast counts the projects while it runs, the figures update in place, and the
toast ends as a summary (a skipped project is counted, a failed read is red and
named). A figure that was not measured shows its word (unknown, unavailable),
never zero.

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


def _run(*, done: int, total: int = 26, finished: bool = False, results: Optional[List[Dict[str, Any]]] = None,
         started_at: str = "2026-11-04T10:00:00Z") -> Dict[str, Any]:
    """The server's progress for one refresh: the skipped project first, then each read."""
    return {
        "started_at": started_at, "finished_at": "2026-11-04T10:01:00Z" if finished else None,
        "total": total, "done": done, "current": f"project-{done}", "error": None,
        "results": results if results is not None else
        [{"repo": "local-only", "status": "skipped", "detail": "no GitHub remote"}],
    }


class _Progress:
    """Stands in for ``GET /api/progress``: the cache, a refresh that runs until released.

    ``run`` is the progress a polling read sees while the refresh is running, so a test
    moves the count by setting it; the released answer carries the finished run.
    """

    def __init__(self, cached: Dict[str, Any], refreshed: Dict[str, Any]) -> None:
        self.cached = cached
        self.refreshed = refreshed
        self.released = False
        self.run: Optional[Dict[str, Any]] = None
        self.requests: List[str] = []

    def __call__(self, route) -> None:
        url = route.request.url
        self.requests.append(url)
        if self.released:
            body = self.refreshed
        elif "refresh=true" in url:
            body = dict(self.cached, refreshing=True)
        else:
            running = self.cached.get("refreshing", False) or self._running()
            body = dict(self.cached, refreshing=running)
            if running and self.run is not None:
                body["github"] = dict(self.cached["github"], refreshing=True, run=self.run)
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


def test_kebab_menu_keeps_usage_details_and_history_rereads_the_cache_by_bucket_and_range(
    authed_page: Page, base_url: str
) -> None:
    """The kebab holds Usage details and History; a bucket or range change in
    the History sheet rereads the cache only, never a new recalculation."""
    page = authed_page
    progress = _Progress(_payload(added=100, refreshing=False), _payload(added=100))
    progress.released = True
    _boot(page, base_url, progress)

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
    expect(page.locator("#progressRefreshed")).to_contain_text("Last refreshed")
    # Opening is a plain read: no refresh call, and no toast.
    expect(_tile(page, "lines")).to_contain_text("+150")
    assert [u for u in progress.requests if "refresh=true" in u] == [], progress.requests
    expect(page.locator("#progressToast")).to_be_hidden()

    before = len(progress.requests)
    page.locator('#progressSheet [data-bucket="week"]').click()
    wait_until(page, lambda: len(progress.requests) > before, "the week read")
    assert "bucket=week" in progress.requests[-1]
    assert "refresh=true" not in progress.requests[-1]

    page.locator('#progressSheet [data-range="90"]').click()
    wait_until(page, lambda: "bucket=week" in progress.requests[-1] and len(progress.requests) > before + 1, "the 90d read")
    expect(page.locator('#progressSheet [data-range="90"]')).to_have_attribute("aria-pressed", "true")

    page.locator("#progressSheetClose").click()
    expect(page.locator("#progressSheet")).not_to_be_visible()


def _tap_refresh(page: Page, progress: _Progress) -> None:
    page.locator("#progressRefresh").click()
    wait_until(page, lambda: any("refresh=true" in u for u in progress.requests), "the refresh ask")


def _poll(page: Page, done) -> None:
    """Jump the fake clock past a poll until ``done()`` holds."""
    def landed() -> bool:
        _fake_clock.advance(page, 2000)
        return done()
    wait_until(page, landed, "the sheet to read the next poll")


def test_refresh_counts_projects_then_ends_in_a_neutral_summary_that_counts_the_skip(
    authed_page: Page, base_url: str
) -> None:
    """A tap on Refresh shows the count while it runs; the summary counts the skipped
    project and is not an error. Updates in place; opening asked for nothing."""
    page = authed_page
    _fake_clock.install(page)
    progress = _Progress(_payload(added=100), _payload(added=250, age=5.0))
    _boot(page, base_url, progress)

    _open_history(page)
    expect(_tile(page, "lines")).to_contain_text("+150")
    expect(page.locator('#progressSources li[data-source="github"]')).to_contain_text("GitHub")
    toast = page.locator("#progressToast")
    expect(toast).to_be_hidden()
    assert [u for u in progress.requests if "refresh=true" in u] == []

    progress.run = _run(done=0)
    _tap_refresh(page, progress)
    expect(toast).to_be_visible()
    expect(toast).to_contain_text("Checking 1 of 26")
    expect(toast).not_to_have_class(re.compile(r"\berror\b"))
    expect(page.locator("#progressRefresh")).to_be_disabled()
    # The sheet is not held: the cached figures stay readable and the controls work.
    expect(_tile(page, "lines")).to_contain_text("+150")

    progress.run = _run(done=1)
    _poll(page, lambda: "Checking 2 of 26" in toast.inner_text())
    progress.run = _run(done=24)
    _poll(page, lambda: "Checking 25 of 26" in toast.inner_text())

    # It ends: the figures update in place and the toast sums up, neutral.
    refreshed = _payload(added=250, age=5.0)
    ok = [{"repo": f"p{i}", "status": "ok", "detail": None} for i in range(25)]
    refreshed["github"]["run"] = _run(done=25, finished=True, results=[
        {"repo": "local-only", "status": "skipped", "detail": "no GitHub remote"}, *ok])
    progress.refreshed = refreshed
    progress.released = True
    _poll(page, lambda: "+300" in _tile(page, "lines").inner_text())
    expect(toast).to_have_text(re.compile(r"Refreshed 25 of 26 · 1 skipped \(no GitHub remote\)"))
    expect(toast).not_to_have_class(re.compile(r"\berror\b"))
    expect(page.locator("#progressRefresh")).to_be_enabled()
    expect(page.locator("#progressRefreshed")).to_contain_text("Last refreshed")

    # One tap, one recalculation.
    asks = [u for u in progress.requests if "refresh=true" in u]
    assert len(asks) == 1, asks

    # A second refresh where reads really failed: red, naming the projects.
    progress.released = False
    progress.cached = progress.refreshed
    progress.run = _run(done=0, started_at="2026-11-04T11:00:00Z")
    _tap_refresh(page, progress)
    failed = _payload(added=250, age=5.0)
    failed["github"]["run"] = _run(done=2, total=4, finished=True, started_at="2026-11-04T11:00:00Z", results=[
        {"repo": "local-only", "status": "skipped", "detail": "no GitHub remote"},
        {"repo": "atlas-web", "status": "ok", "detail": None},
        {"repo": "beacon-api", "status": "failed", "detail": "GhError: boom"},
        {"repo": "cinder-cli", "status": "failed", "detail": "GhError: boom"}])
    progress.refreshed = failed
    progress.released = True
    _poll(page, lambda: "could not read" in toast.inner_text())
    expect(toast).to_have_class(re.compile(r"\berror\b"))
    expect(toast).to_have_text(re.compile(
        r"Refreshed 1 of 4 · 1 skipped \(no GitHub remote\) · could not read beacon-api, cinder-cli"))


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
def test_sheet_fits_a_phone_and_legend_marks_are_the_agents_brand_colour_alone(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    page.set_viewport_size({"width": 390, "height": 844})
    cached = _payload(added=100)
    cached["github"]["unknown_repos"] = ["one", "two", "three", "four", "five"]
    progress = _Progress(cached, cached)
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

    marks = page.locator("#progressTokensLegend .progress-legend-mark")
    expect(marks).to_have_count(1)
    # Colour only: no icon, no letter.
    expect(marks.first).to_have_text("")
    expect(marks.first.locator("*")).to_have_count(0)
    # The swatch is the agent's brand token, the one its chart series paints.
    brand = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--brand-claude').trim()")
    assert brand, "--brand-claude must resolve"
    swatch = marks.first.evaluate("m => getComputedStyle(m).backgroundColor")
    probe = page.evaluate(
        "c => { const e = document.createElement('i'); e.style.color = c; document.body.appendChild(e);"
        " const v = getComputedStyle(e).color; e.remove(); return v; }", brand)
    assert swatch == probe, (swatch, probe)
    # The per-agent rows keep their icon.
    expect(page.locator('#progressAgents .progress-agent-row[data-agent="claude"] .agent-icon')).to_have_count(1)

    # At 360 px, in both themes, the By repo title keeps one line with its meta
    # below it as a subtitle, never drawn over it (the Android report).
    page.set_viewport_size({"width": 360, "height": 740})
    for theme in ("light", "dark"):
        page.emulate_media(color_scheme=theme)
        title = page.locator("#progressReposTitle")
        meta = page.locator("#progressReposMeta")
        title.scroll_into_view_if_needed()
        expect(meta).to_contain_text("not read yet")
        t, m = title.bounding_box(), meta.bounding_box()
        assert t is not None and m is not None
        assert m["y"] >= t["y"] + t["height"] - 1, (theme, t, m)
        line = title.evaluate("e => parseFloat(getComputedStyle(e).lineHeight) || e.getBoundingClientRect().height")
        assert t["height"] <= line * 1.5, (theme, t, line)
        host = dialog.bounding_box()
        assert host is not None and m["x"] + m["width"] <= host["x"] + host["width"] + 1, (theme, m, host)
