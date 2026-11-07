"""The Jobs list keeps the phone's scroll position across a poll (#2212).

Every 4 s poll re-rendered the whole list, and on a phone the page snapped back
to the top while the owner was reading further down. The mocked jobs change
between ticks (a run starts, a count moves) so the poll has something real to
repaint; the clock is the page's own, jumped one poll at a time (#1376).
Every endpoint is route-mocked before navigation (#510); no job is run.
All names are fictional.
"""

from __future__ import annotations

import json
import re
import time

import pytest
from playwright.sync_api import Page, expect

from tests.e2e import _fake_clock

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_NOW = int(time.time())
_COUNT = 40
_POLL_MS = 4000


def _job(index: int, running: bool) -> dict:
    return {
        "id": "job%02d" % index, "name": "Fictional job %02d" % index,
        "target_kind": "py", "schedule_chip": "daily 03:00",
        "next_run": "tomorrow 03:00", "next_run_epoch": _NOW + 3600 * (index + 1),
        "running": running, "stuck": False, "paused": False, "confirm": False,
        "params": [], "schedule": {"type": "daily", "at": "03:00"},
        # A failing job puts an exception in the page header, which the poll
        # then measures to fit (home-head.js fitHeadStatus).
        "last_run": {"run_id": "r1", "status": "failed", "outcome": "failed",
                     "started_at": "2026-01-01T09:00:00"} if index == 0 else None,
        "stats": {"last7": []},
    }


def _fulfill(body):
    return lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(body))


def _scroll_top(page: Page) -> int:
    return page.evaluate("() => Math.round(window.scrollY)")


def test_scroll_position_holds_across_a_poll_that_changes_the_data(
    authed_page: Page, base_url: str
) -> None:
    page = authed_page
    tick = {"n": 0}

    def jobs(route):
        # The last job starts running on the second answer: a live status
        # change the poll must still paint.
        _fulfill({"jobs": [_job(i, running=(i == _COUNT - 1 and tick["n"] > 0))
                           for i in range(_COUNT)]})(route)

    # The phone layout scrolls the document; the wide one scrolls main.app.
    page.set_viewport_size({"width": 390, "height": 740})
    _fake_clock.install(page)
    page.route(re.compile(r".*/api/jobs(\?.*)?$"), jobs)
    page.route(re.compile(r".*/api/jobs/agenda(\?.*)?$"),
               _fulfill({"days": 7, "generated_epoch": _NOW, "occurrences": [], "frequent": []}))
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabJobs").click()
    rows = page.locator("#jobsList li.job-row")
    expect(rows).to_have_count(_COUNT, timeout=10_000)

    page.evaluate("() => window.scrollTo(0, document.documentElement.scrollHeight)")
    before = _scroll_top(page)
    assert before > 200, "the list is too short to scroll, the test proves nothing"

    tick["n"] = 1
    for _ in range(2):
        _fake_clock.jump_interval(page, _POLL_MS, "fetchJobs")
    last = page.locator('#jobsList li.job-row[data-id="job%02d"]' % (_COUNT - 1))
    expect(last.locator(".job-avatar")).to_have_attribute("data-badge", "alive")
    assert _scroll_top(page) == before
