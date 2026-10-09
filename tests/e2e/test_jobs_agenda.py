"""Regression pin for issue #230 (Jobs tab: the schedule agenda), rebuilt by #1438.

The agenda used to be a collapsed ``<details>`` Schedule card above the job
list. #1438 split it in two, both fed by one ``GET /api/jobs/agenda`` payload:
a **Next up** card (the next three fires, visible without a tap) and the full
7-day agenda in a **sheet** the card's header opens. Both show time-ordered
fires; the sheet groups them by day (``Today`` / ``Tomorrow`` / weekday) with a
"frequent" footer for dense minutes/hourly jobs. Tapping a fire used to expand
that job in the list below (the accordion is gone); it now closes the sheet and
opens the job's sheet. The agenda is fetched once per visit to the tab, never
by the 4s jobs poll and never by opening the sheet.

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

from tests.e2e._contrast import contrast_ratio
from tests.e2e.conftest import flush_requests, stable_read

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


def _wire(page: Page, agenda=_AGENDA, now: _dt.datetime = _ANCHOR) -> list:
    """Mock the endpoints; returns the list that records each agenda fetch."""
    # Freeze the page's `new Date()` at the same anchor the fixture epochs are
    # built from (#918). `set_fixed_time` pins Date only — timers keep running,
    # so boot and the panel's fetch are unaffected.
    page.clock.set_fixed_time(now)
    agenda_calls: list = []

    def _agenda_route(route):
        agenda_calls.append(route.request.url)
        route.fulfill(status=200, content_type="application/json",
                      body=_json.dumps(agenda))

    page.route(re.compile(r".*/api/jobs/agenda(\?.*)?$"), _agenda_route)
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
    return agenda_calls


def _open_agenda(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabJobs").click()
    expect(page.locator("#jobsNextUpCard")).to_have_attribute("data-state", "ready")
    page.locator("#jobsAgendaOpen").click()
    expect(page.locator("#jobsAgendaSheet")).to_be_visible()
    page.wait_for_selector("#jobsAgendaBody .jobs-fire-row", state="attached", timeout=5_000)


def _day_headers(page: Page):
    return page.eval_on_selector_all(
        "#jobsAgendaBody .jobs-agenda-day", "els => els.map(e => e.textContent)")


@pytest.mark.iphone
def test_agenda_groups_by_day_in_order(authed_page: Page, base_url: str) -> None:
    agenda_calls = _wire(authed_page)
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabJobs").click()

    # Next up (#1438): the first three fires, no tap needed — time, name, and
    # "<day> · <Cadence>" (the cadence's first letter lifted).
    nxt = authed_page.locator("#jobsNextUpList li.jobs-fire-row")
    expect(nxt).to_have_count(3)
    assert nxt.evaluate_all("els => els.map(e => e.dataset.jobId)") == ["alpha", "zeta", "alpha"]
    expect(nxt.locator(".jobs-fire-time")).to_have_text(["13:00", "01:00", "07:00"])
    expect(nxt.locator(".action-row-meta")).to_have_text(
        ["today · Daily 13:00", "tomorrow · Daily 01:00", "tomorrow · Daily 13:00"])

    authed_page.locator("#jobsAgendaOpen").click()
    expect(authed_page.locator("#jobsAgendaSheet")).to_be_visible()
    headers = _day_headers(authed_page)
    assert headers[0] == "Today"
    assert "Tomorrow" in headers

    ids = authed_page.eval_on_selector_all(
        "#jobsAgendaBody .jobs-fire-row", "els => els.map(e => e.dataset.jobId)")
    assert ids == ["alpha", "zeta", "alpha"], "rows must be time-ordered across days"
    # Each fire is a tap target (it opens its job): the 44px floor, not the
    # 38px its padding alone gave (#1174).
    heights = authed_page.eval_on_selector_all(
        "#jobsAgendaBody .jobs-fire-row .action-row-main",
        "els => els.map(e => e.getBoundingClientRect().height)")
    assert all(h >= 43.99 for h in heights), f"agenda rows under 44px: {heights}"
    # The time reads at 4.5:1 in both themes, on the row's hover wash too: a
    # tap leaves :hover stuck on a phone (#1175, COLOR-02: the dark theme's
    # link accent measured 4.21:1 there). The time is the fire row's leading
    # value since #1438 (was the accent `.jobs-agenda-time`); the meta text
    # beside it is held to the same floor.
    row = authed_page.locator("#jobsAgendaBody .jobs-fire-row").first
    row.hover()
    for theme in ("light", "dark"):
        authed_page.evaluate(f"document.documentElement.dataset.theme = '{theme}'")
        for part in (".jobs-fire-time", ".action-row-meta"):
            ratio = stable_read(lambda p=part: contrast_ratio(row.locator(p)))
            assert ratio >= 4.5, f"{theme}: agenda {part} at {ratio:.2f}:1, under 4.5:1"
    authed_page.evaluate("delete document.documentElement.dataset.theme")

    # Dense cadences are summarised, not expanded into the list.
    expect(authed_page.locator(".jobs-agenda-frequent")).to_contain_text("Mango")

    # One fetch per visit: neither opening the sheet nor the 4s jobs poll asks
    # again (#1438).
    flush_requests(authed_page)
    assert len(agenda_calls) == 1, agenda_calls

    # -- was test_agenda_row_reveals_job (merged in #1215; last: it leaves the
    # sheet). The fire used to expand that job in the list below; since #1438
    # it closes the agenda sheet and opens the job's own sheet. --
    authed_page.locator(
        "#jobsAgendaBody .jobs-fire-row[data-job-id='zeta'] .action-row-main").click()
    expect(authed_page.locator("#jobsAgendaSheet")).to_be_hidden()
    expect(authed_page.locator("#jobSheet")).to_be_visible()
    expect(authed_page.locator("#jobSheetTitle")).to_have_text("Zeta")
    expect(authed_page.locator("#jobSheetBody")).to_have_attribute("data-job-id", "zeta")


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

    assert _day_headers(authed_page) == ["Today", "Tomorrow"]

    ids = authed_page.eval_on_selector_all(
        "#jobsAgendaBody .jobs-fire-row", "els => els.map(e => e.dataset.jobId)")
    assert ids == ["alpha", "zeta"]
    # Next up draws its day from the same clock (the meta line's first word).
    expect(authed_page.locator("#jobsNextUpList .action-row-meta")).to_have_text(
        ["today · Daily 23:45", "tomorrow · Daily 00:15"])


def test_agenda_empty_state(authed_page: Page, base_url: str) -> None:
    _wire(authed_page, agenda={"days": 7, "generated_epoch": _epoch(0),
                               "occurrences": [], "frequent": []})
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabJobs").click()
    # Next up says why it is empty (design.md "Async data & feedback").
    expect(authed_page.locator("#jobsNextUpCard")).to_have_attribute("data-state", "empty")
    expect(authed_page.locator("#jobsNextUpState")).to_contain_text(
        "Nothing scheduled in the next 7 days")
    authed_page.locator("#jobsAgendaOpen").click()
    expect(authed_page.locator("#jobsAgendaBody")).to_contain_text(
        "No scheduled runs in the next 7 days")
    # Its own next step (#1201): an Add job button that opens the same
    # dialog as the Jobs card's +. (Edit mode is gone: that + is always shown,
    # #1438.)
    expect(authed_page.locator("#jobsAddBtn")).to_be_visible()
    add = authed_page.locator("#jobsAgendaBody .empty-state-action")
    expect(add).to_have_text("Add job")
    add.click()
    expect(authed_page.locator("#jobsAgendaSheet")).to_be_hidden()
    expect(authed_page.locator("#jobDialog")).to_be_visible()
    expect(authed_page.locator("#jobDialogTitle")).to_have_text("Add job")
