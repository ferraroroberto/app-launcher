"""The Jobs tab on the Glance system (#1438, step 6/7 of #1432).

A Next up card with the next three fires (its header opens the 7-day agenda
in a sheet), rows on the shared anatomy with Run now, Pause/Resume, Dry-run,
Edit and Remove in one kebab and no Edit mode anywhere (decisions 5 and 6),
and a job sheet in place of the inline accordion with Run now as its primary
action, whose output streams there (decision 7). On the wide desktop layout
the sheet docks beside the list. The page header shows only the exceptions.

Every endpoint the tab reads is route-mocked before navigation (#510), and
the run stream is a mocked WebSocket: no real job is run, paused or edited.
All names are fictional.
"""

from __future__ import annotations

import json
import re
import time

import pytest
from playwright.sync_api import Page, WebSocketRoute, expect

from tests.e2e.conftest import flush_requests, stable_eval, wait_until

pytestmark = pytest.mark.smoke

_NOW = int(time.time())


def _last7(*outcomes):
    return [{"run_id": "h%d" % i, "status": o, "outcome": o} for i, o in enumerate(outcomes)]


def _job(job_id: str, name: str, **extra) -> dict:
    job = {
        "id": job_id, "name": name, "target_kind": "py",
        "schedule_chip": "daily 03:00", "next_run": "tomorrow 03:00",
        "next_run_epoch": _NOW + 6 * 3600, "running": False, "stuck": False,
        "paused": False, "confirm": False, "params": [],
        "schedule": {"type": "daily", "at": "03:00"},
        "stats": {"last7": _last7(*["success"] * 7)},
    }
    job.update(extra)
    return job


_JOBS = [
    _job("ledger", "Ledger reconciliation", alert_on_failure=True),
    _job("mailbox", "Mailbox sweep", schedule_chip="every 15 min",
         next_run_epoch=_NOW + 300,
         last_run={"run_id": "m1", "status": "failed", "outcome": "failed",
                   "started_at": "2026-01-01T09:00:00"}),
    _job("archive", "Photo library backup run", schedule_chip="daily 01:00",
         running=True,
         stats={"last7": _last7(*["success"] * 6, "running")}),
    _job("planner", "Planner sync", coverage={"state": "problem", "detail": "no task"}),
    _job("guarded", "Guarded export", confirm=True),
    _job("shaped", "Shaped report",
         params=[{"name": "month", "type": "str", "required": False, "default": ""}]),
]

_AGENDA = {"days": 7, "generated_epoch": _NOW, "occurrences": [
    {"job_id": "ledger", "name": "Ledger reconciliation",
     "fire_epoch": _NOW + 6 * 3600, "cadence": "daily 03:00"},
    {"job_id": "archive", "name": "Photo library backup run",
     "fire_epoch": _NOW + 7 * 3600, "cadence": "daily 01:00"},
    {"job_id": "planner", "name": "Planner sync",
     "fire_epoch": _NOW + 8 * 3600, "cadence": "daily 03:00"},
    {"job_id": "ledger", "name": "Ledger reconciliation",
     "fire_epoch": _NOW + 30 * 3600, "cadence": "daily 03:00"},
], "frequent": [{"job_id": "mailbox", "name": "Mailbox sweep", "cadence": "every 15 min"}]}

_RUNS = [
    {"run_id": "r2", "status": "success", "outcome": "success",
     "started_at": "2026-01-02T03:00:00", "trigger_source": "schtasks"},
    {"run_id": "r1", "status": "failed", "outcome": "failed",
     "started_at": "2026-01-01T03:00:00", "trigger_source": "schtasks"},
]


def _fulfill(body):
    return lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(body))


def _open_jobs(page: Page, base_url: str, jobs=None) -> dict:
    """Mock the tab's endpoints, open it, and return the request log."""
    seen = {"deleted": [], "fired": []}

    def job_or_delete(route):
        if route.request.method == "DELETE":
            seen["deleted"].append(route.request.url)
            return _fulfill({"ok": True})(route)
        return route.fallback()

    def fire(route):
        seen["fired"].append(route.request.url)
        _fulfill({"run_id": "r3", "job_id": "ledger"})(route)

    def runs(route):
        listed = _RUNS
        if seen["fired"]:
            listed = [{"run_id": "r3", "status": "running", "outcome": "running",
                       "started_at": "2026-01-03T03:00:00", "trigger_source": "api"}] + _RUNS
        _fulfill({"runs": listed})(route)

    def run_detail(route):
        run_id = route.request.url.rsplit("/", 1)[-1]
        status = "running" if run_id == "r3" else "success"
        _fulfill({"run": {"run_id": run_id, "status": status,
                          "output_tail": "output of " + run_id}})(route)

    page.route(re.compile(r".*/api/jobs(\?.*)?$"), _fulfill({"jobs": jobs or _JOBS}))
    page.route(re.compile(r".*/api/jobs/agenda(\?.*)?$"), _fulfill(_AGENDA))
    page.route(re.compile(r".*/api/jobs/[^/]+$"), job_or_delete)
    page.route(re.compile(r".*/api/jobs/[^/]+/run(\?.*)?$"), fire)
    page.route(re.compile(r".*/api/jobs/[^/]+/runs$"), runs)
    page.route(re.compile(r".*/api/jobs/[^/]+/runs/[^/]+$"), run_detail)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabJobs").click()
    expect(page.locator("#jobsList li.job-row")).to_have_count(len(jobs or _JOBS), timeout=10_000)
    return seen


def _row(page: Page, job_id: str):
    return page.locator(f'#jobsList li.job-row[data-id="{job_id}"]')


def _menu(page: Page, job_id: str):
    _row(page, job_id).locator(".job-menu-anchor").click()
    return _row(page, job_id).locator(".row-menu")


def test_header_next_up_and_the_agenda_sheet(authed_page: Page, base_url: str) -> None:
    page = authed_page
    _open_jobs(page, base_url)

    # The header: the exceptions in their tones, failing first.
    head = page.locator("#jobsHeadStatus")
    expect(head).to_have_text("1 failing · 1 not firing")
    expect(head.locator(".head-exception").first).to_have_attribute("data-tone", "danger")
    expect(head.locator(".head-exception").last).to_have_attribute("data-tone", "attention")

    # Next up is the first card and shows three fires without a tap.
    expect(page.locator("#paneJobs > .card").nth(1)).to_have_id("jobsNextUpCard")
    card = page.locator("#jobsNextUpCard")
    expect(card).to_have_attribute("data-state", "ready")
    fires = page.locator("#jobsNextUpList li.jobs-fire-row")
    expect(fires).to_have_count(3)
    expect(fires.first.locator(".jobs-fire-time")).to_have_text(re.compile(r"^\d\d:\d\d$"))
    expect(fires.first.locator(".action-row-title")).to_have_text("Ledger reconciliation")
    expect(fires.first.locator(".action-row-meta")).to_have_text(
        re.compile(r"^(today|tomorrow) · Daily 03:00$"))

    # The header's chevron opens the 7-day agenda: day groups, all fires, the
    # frequent footer.
    page.locator("#jobsAgendaOpen").click()
    sheet = page.locator("#jobsAgendaSheet")
    expect(sheet).to_be_visible()
    expect(sheet.locator("li.jobs-fire-row")).to_have_count(4)
    expect(sheet.locator(".jobs-agenda-day").first).to_be_visible()
    expect(sheet.locator(".jobs-agenda-frequent")).to_have_text(
        "Also frequent: Mailbox sweep (every 15 min)")

    # A fire opens that job's sheet, in place of the agenda.
    sheet.locator('li.jobs-fire-row[data-job-id="planner"] .action-row-main').click()
    expect(sheet).to_be_hidden()
    expect(page.locator("#jobSheet")).to_be_visible()
    expect(page.locator("#jobSheetTitle")).to_have_text("Planner sync")


def test_header_falls_back_to_the_count(authed_page: Page, base_url: str) -> None:
    _open_jobs(authed_page, base_url, jobs=[_job("ledger", "Ledger reconciliation"),
                                            _job("planner", "Planner sync")])
    head = authed_page.locator("#jobsHeadStatus")
    expect(head).to_have_text("2 jobs")
    expect(head.locator(".head-exception")).to_have_count(0)


def test_kebab_runs_and_removes_with_no_edit_mode(authed_page: Page, base_url: str) -> None:
    page = authed_page
    seen = _open_jobs(page, base_url)

    # No Edit mode anywhere, and Add is always there.
    expect(page.locator("#jobsEditBtn")).to_have_count(0)
    expect(page.locator("#jobsAddBtn")).to_be_visible()
    # No per-row Run button: the row is the main button and one kebab.
    expect(_row(page, "ledger").locator(":scope > button")).to_have_count(2)

    # Run now, the kebab's first item (test_jobs_row_anatomy pins the set),
    # fires straight away for a plain job.
    _menu(page, "ledger").locator(".job-run-item").click()
    wait_until(page, lambda: len(seen["fired"]) == 1, "the run POST")
    assert "confirmed" not in seen["fired"][0]

    # A running job cannot be run again from its kebab.
    expect(_menu(page, "archive").locator(".job-run-item")).to_be_disabled()
    page.keyboard.press("Escape")

    # A confirm job asks through the vendored dialog first.
    _menu(page, "guarded").locator(".job-run-item").click()
    confirm = page.locator("#confirmDialog")
    expect(confirm).to_be_visible()
    expect(page.locator("#confirmDialogTitle")).to_have_text("Run Guarded export?")
    page.locator("#confirmDialogOk").click()
    wait_until(page, lambda: len(seen["fired"]) == 2, "the confirmed run POST")
    assert "confirmed=1" in seen["fired"][1]

    # A job with params opens its run dialog instead of firing.
    _menu(page, "shaped").locator(".job-run-item").click()
    expect(page.locator("#jobRunDialog")).to_be_visible()
    page.locator("#jobRunCancel").click()
    flush_requests(page)
    assert len(seen["fired"]) == 2

    # Remove asks through the vendored dialog; dismissing it removes nothing.
    _menu(page, "planner").locator(".job-remove-item").click()
    expect(page.locator("#confirmDialogTitle")).to_have_text("Remove Planner sync?")
    page.locator("#confirmDialogClose").click()
    flush_requests(page)
    assert seen["deleted"] == []
    _menu(page, "planner").locator(".job-remove-item").click()
    page.locator("#confirmDialogOk").click()
    wait_until(page, lambda: len(seen["deleted"]) == 1, "the DELETE")
    assert seen["deleted"][0].endswith("/api/jobs/planner")


def test_sheet_runs_now_and_streams_the_new_run(authed_page: Page, base_url: str) -> None:
    page = authed_page

    def stream(ws: WebSocketRoute) -> None:
        ws.send(json.dumps({"type": "snapshot", "status": "running",
                            "output": "streamed line"}))

    page.route_web_socket(re.compile(r".*/api/jobs/ledger/runs/r3/stream.*"), stream)
    seen = _open_jobs(page, base_url)

    _row(page, "ledger").locator(".action-row-main").click()
    sheet = page.locator("#jobSheet")
    expect(sheet).to_be_visible()
    expect(_row(page, "ledger")).to_have_attribute("aria-current", "true")
    # Every former accordion section, the Alerts line among the details.
    body = page.locator("#jobSheetBody")
    expect(body.locator("[data-role='job-details']")).to_be_visible()
    expect(body.locator("[data-role='alerts-line']")).to_contain_text("Telegram")
    expect(body.locator("[data-role='runs-list'] .jobs-run-btn")).to_have_count(2)
    expect(body.locator("[data-role='output-tail']")).to_have_text("output of r2")

    run = page.locator("#jobSheetRun")
    expect(run).to_have_text("Run now")
    expect(run).to_have_class(re.compile(r"\bbutton-primary\b"))
    run.click()
    wait_until(page, lambda: len(seen["fired"]) == 1, "the run POST")
    # The sheet follows the run it started, and its output streams in.
    expect(body.locator(".jobs-run-btn.selected")).to_contain_text("running")
    expect(body.locator("[data-role='output-tail']")).to_have_text("streamed line")
    expect(body.locator("[data-role='output-label']")).to_contain_text("r3 · running (live)")

    page.locator("#jobSheetClose").click()
    expect(sheet).to_be_hidden()
    expect(_row(page, "ledger")).not_to_have_attribute("aria-current", "true")


def test_search_hit_opens_the_sheet_on_its_run(authed_page: Page, base_url: str) -> None:
    page = authed_page
    page.route(re.compile(r".*/api/jobs/runs/search.*"), _fulfill({"matches": [
        {"job_id": "ledger", "run_id": "r1", "status": "failed", "outcome": "failed",
         "snippet": "Traceback: boom"},
    ]}))
    _open_jobs(page, base_url)
    page.locator("#jobsSearchInput").fill("boom")
    hit = page.locator("#jobsList li.job-search-hit")
    expect(hit).to_have_count(1)
    hit.locator(".action-row-main").click()
    expect(page.locator("#jobSheet")).to_be_visible()
    expect(page.locator("#jobSheetBody .jobs-run-btn.selected")).to_contain_text("failed")
    expect(page.locator("#jobSheetBody [data-role='output-tail']")).to_have_text("output of r1")


@pytest.mark.iphone
def test_a_24_character_name_is_not_cut_at_phone_width(
    authed_page: Page, base_url: str, browser_name: str
) -> None:
    if browser_name != "webkit":
        pytest.skip("desktop Chromium draws a 15px scrollbar a phone does not have")
    page = authed_page
    page.set_viewport_size({"width": 390, "height": 844})
    _open_jobs(page, base_url)
    row = _row(page, "archive")
    m = stable_eval(row, """li => {
      const t = li.querySelector('.action-row-title');
      return { cut: t.scrollWidth > t.clientWidth,
               h: Math.round(li.getBoundingClientRect().height) };
    }""")
    assert not m["cut"], "'Photo library backup run' is ellipsized at 390px"
    assert m["h"] == 60, m


def test_wide_layout_docks_the_sheet_beside_the_list(
    authed_page: Page, base_url: str, browser_name: str
) -> None:
    if browser_name != "chromium":
        pytest.skip("the wide layout needs a fine pointer: desktop Chromium only")
    page = authed_page
    page.set_viewport_size({"width": 1440, "height": 900})
    _open_jobs(page, base_url)
    expect(page.locator("#jobDetailEmpty")).to_be_visible()
    _row(page, "ledger").locator(".action-row-main").click()
    sheet = page.locator("#jobSheet")
    expect(sheet).to_be_visible()
    expect(page.locator("#jobDetailEmpty")).to_be_hidden()
    # Docked, not modal: the list beside it stays usable and another row
    # swaps the job in place.
    assert sheet.evaluate("d => !d.matches(':modal')")
    expect(page.locator(".tabs")).to_be_visible()
    _row(page, "planner").locator(".action-row-main").click()
    expect(page.locator("#jobSheetTitle")).to_have_text("Planner sync")
    expect(_row(page, "planner")).to_have_attribute("aria-current", "true")
    # Leaving the tab closes it.
    page.locator("#tabApps").click()
    expect(sheet).to_be_hidden()
