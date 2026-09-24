"""Regression pin for issue #230 (Jobs tab: foldable schedule agenda view).

The 🗓️ Schedule panel sits above Registered jobs, collapsed by default. On
open it fetches ``/api/jobs/agenda`` and renders upcoming fires as a
day-grouped list (``Today`` / ``Tomorrow`` / weekday), time-ordered, with a
"frequent" footer for dense minutes/hourly jobs. Tapping a row reveals that
job expanded in the list below.

Hermetic: route-mock the agenda + jobs + run-list endpoints with fixed
occurrences anchored to a fixed local midnight, and pin the *browser's* clock
to that same anchor (so the Today/Tomorrow grouping is deterministic
regardless of run time). Both projections.
"""

from __future__ import annotations

import datetime as _dt
import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

# One anchor for both sides of the comparison (#918). `_dayHeader` in
# jobs-agenda.js labels a row by diffing its epoch against the browser's
# `new Date()`, so the fixture's day and the page's "now" have to come from
# the same clock or the labels are a wall-clock race: anchoring the fixture to
# `date.today()` (bound at import) while the page read the real clock at
# assert time turned any gate run straddling local midnight into a red
# ("assert 'Fri 11 Sep' == 'Today'"). A fixed calendar day rather than the
# real one also makes the weekday-name branch deterministic (no test asserts
# a weekday label today, but one added later would not have to move with the
# calendar). Mid-June is far from any plausible host's DST transition; noon
# is far from either day boundary.
_ANCHOR = _dt.datetime(2026, 6, 11, 12, 0)  # a Thursday, local wall clock
_MIDNIGHT = _ANCHOR.replace(hour=0, minute=0)

# Rows: 13:00 on the anchor day, then 01:00 and 07:00 the next day. (The
# client renders what the mock returns; it does not filter by "now", so a
# past time-of-day is fine.)


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


def _wire(page: Page, agenda=_AGENDA, now: _dt.datetime = _ANCHOR) -> None:
    # Freeze the page's `new Date()` at the same anchor the fixture epochs are
    # built from (#918). `set_fixed_time` pins Date only — timers keep running,
    # so boot and the panel's fetch are unaffected.
    page.clock.set_fixed_time(now)
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
    # Each row is a tap target (it reveals its job): the 44px floor, not the
    # 38px its padding alone gave (#1174).
    heights = authed_page.eval_on_selector_all(
        ".jobs-agenda-row", "els => els.map(e => e.getBoundingClientRect().height)")
    assert all(h >= 43.99 for h in heights), f"agenda rows under 44px: {heights}"

    # Dense cadences are summarised, not expanded into the list.
    expect(authed_page.locator(".jobs-agenda-frequent")).to_contain_text("Mango")


def test_agenda_day_labels_hold_across_local_midnight(
    authed_page: Page, base_url: str
) -> None:
    """#918: the boundary, faked rather than waited for.

    Thirty seconds before local midnight, with one row either side of it. The
    labels must still read Today/Tomorrow — under the old wall-clock anchoring
    the page's "now" could land on the far side of 00:00 from the fixture and
    the first header rendered as a bare date.
    """
    late = _MIDNIGHT + _dt.timedelta(hours=23, minutes=59, seconds=30)
    agenda = {
        "days": 7,
        "generated_epoch": _epoch(0),
        "occurrences": [
            {"job_id": "alpha", "name": "Alpha", "fire_epoch": _epoch(23.75),
             "fire_iso": "", "cadence": "daily 23:45"},
            {"job_id": "zeta", "name": "Zeta", "fire_epoch": _epoch(24.25),
             "fire_iso": "", "cadence": "daily 00:15"},
        ],
        "frequent": [],
    }
    _wire(authed_page, agenda=agenda, now=late)
    _open_agenda(authed_page, base_url)

    headers = authed_page.eval_on_selector_all(
        ".jobs-agenda-day", "els => els.map(e => e.textContent)")
    assert headers == ["Today", "Tomorrow"]

    ids = authed_page.eval_on_selector_all(
        ".jobs-agenda-row", "els => els.map(e => e.dataset.jobId)")
    assert ids == ["alpha", "zeta"]


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
    # A next step, naming the controls that add one (#1191).
    expect(authed_page.locator("#jobsAgendaBody")).to_contain_text(
        "set a job's Schedule in Edit mode under Registered jobs")
