"""Regression pin for issue #230 (Jobs tab: foldable schedule agenda view).

The 🗓️ Schedule panel sits above Registered jobs, collapsed by default. On
open it fetches ``/api/jobs/agenda`` and renders upcoming fires as a
day-grouped list (``Today`` / ``Tomorrow`` / weekday), time-ordered, with a
"frequent" footer for dense minutes/hourly jobs. Tapping a row reveals that
job expanded in the list below.

Hermetic: route-mock the agenda + jobs + run-list endpoints with fixed
occurrences anchored to local midnight (so the Today/Tomorrow grouping is
deterministic regardless of run time). Both projections.
"""

from __future__ import annotations

import datetime as _dt
import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

# Anchor to local midnight so calendar-day grouping is fixed: 13:00 today,
# then 01:00 and 07:00 tomorrow. (The client renders what the mock returns;
# it does not filter by "now", so a past time-of-day is fine.)
_MIDNIGHT = _dt.datetime.combine(_dt.date.today(), _dt.time(0, 0))


def _epoch(hours: float) -> int:
    return int((_MIDNIGHT + _dt.timedelta(hours=hours)).timestamp())


_AGENDA = {
    "days": 7,
    "generated_epoch": _epoch(0),
    "occurrences": [
        {"job_id": "alpha", "name": "Alpha", "fire_epoch": _epoch(13),
         "fire_iso": "", "cadence": "daily 13:00"},
        {"job_id": "zeta", "name": "Zeta", "fire_epoch": _epoch(25),
         "fire_iso": "", "cadence": "daily 01:00"},
        {"job_id": "alpha", "name": "Alpha", "fire_epoch": _epoch(31),
         "fire_iso": "", "cadence": "daily 13:00"},
    ],
    "frequent": [{"job_id": "mango", "name": "Mango", "cadence": "every 5 min"}],
}


def _job(job_id, name):
    return {
        "id": job_id, "name": name, "target_kind": "py", "schedule_chip": "daily",
        "next_run": None, "next_run_epoch": None, "next_run_iso": None,
        "running": False, "stuck": False, "paused": False, "args": "",
        "schedule": {"type": "daily", "at": "13:00"}, "params": [],
        "last_run": None,
        "stats": {"p50": None, "p95": None, "success_rate_30d": None,
                  "completed_count": 0, "last7": []},
    }


def _wire(page: Page, agenda=_AGENDA) -> None:
    page.route(
        re.compile(r".*/api/jobs/agenda(\?.*)?$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(agenda)),
    )
    page.route(
        re.compile(r".*/api/jobs/[^/]+/runs$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps({"runs": []})),
    )
    page.route(
        re.compile(r".*/api/jobs(\?.*)?$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"jobs": [_job("alpha", "Alpha"), _job("zeta", "Zeta")]})),
    )


def _open_agenda(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabJobs").click()
    card = page.locator("#jobsAgendaCard")
    card.wait_for(state="attached", timeout=5_000)
    assert not card.evaluate("el => el.open"), "agenda panel must be collapsed by default"
    page.locator("#jobsAgendaCard summary").click()
    page.wait_for_selector(".jobs-agenda-row", state="attached", timeout=5_000)


def test_agenda_groups_by_day_in_order(authed_page: Page, base_url: str) -> None:
    _wire(authed_page)
    _open_agenda(authed_page, base_url)

    headers = authed_page.eval_on_selector_all(
        ".jobs-agenda-day", "els => els.map(e => e.textContent)")
    assert headers[0] == "Today"
    assert "Tomorrow" in headers

    ids = authed_page.eval_on_selector_all(
        ".jobs-agenda-row", "els => els.map(e => e.dataset.jobId)")
    assert ids == ["alpha", "zeta", "alpha"], "rows must be time-ordered across days"

    # Dense cadences are summarised, not expanded into the list.
    expect(authed_page.locator(".jobs-agenda-frequent")).to_contain_text("Mango")


def test_agenda_row_reveals_job(authed_page: Page, base_url: str) -> None:
    _wire(authed_page)
    _open_agenda(authed_page, base_url)

    authed_page.locator(".jobs-agenda-row[data-job-id='zeta']").first.click()
    # The reveal expands that job's history <li> in the Registered-jobs list.
    expect(
        authed_page.locator("#jobsList li.jobs-history-li[data-history-for='zeta']")
    ).to_be_visible()


def test_agenda_empty_state(authed_page: Page, base_url: str) -> None:
    _wire(authed_page, agenda={"days": 7, "generated_epoch": _epoch(0),
                               "occurrences": [], "frequent": []})
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabJobs").click()
    authed_page.locator("#jobsAgendaCard summary").click()
    expect(authed_page.locator("#jobsAgendaBody")).to_contain_text(
        "No scheduled runs in the next 7 days")
