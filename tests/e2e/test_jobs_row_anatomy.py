"""Regression pin for the Jobs row anatomy (#1130, rebuilt by #1438).

#1130 cut a row that carried about fourteen data points across five lines to
two lines and one action. #1438 (step 6/7 of #1432) moved the row onto the
shared action-row anatomy the other tabs use, so this pins the *new* contract:

* a 60px row at 390px: job avatar, the name, **one** meta line ("in 6h · Daily
  03:00"), chips for exceptions only (a healthy job has none), the seven-dot
  history as the trailing value, and one kebab;
* no per-row Run button, no bell, no status dot, no countdown / cadence pills:
  Run now is the kebab's first item and the sheet's primary action, the bell
  is the sheet's "Alerts" line, the countdown and cadence are the meta line;
* a running job says so with the avatar's alive badge, and its live history
  dot is drawn in the accent, never the amber of "not confirmed";
* everything demoted is reachable in the job sheet the row opens (the other
  half of the contract: a later trim cannot quietly drop a fact);
* the kebab holds Run now, Pause, Dry-run check, Edit and Remove, Remove last.

The inline accordion this test used to open, and the Edit mode that gated the
menu's Edit / Remove, are gone (#1438).
"""
from __future__ import annotations

import json as _json
import re
import time

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import stable_eval, stable_read

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_JOB = {
    "id": "demo", "name": "Nightly reconciliation", "script_path": "E:/demo/run.py",
    "kind": "", "target_kind": "py", "schedule_chip": "daily 03:00",
    "next_run": "2026-09-23 03:00", "running": False, "stuck": False,
    "args": "", "confirm": False, "paused": False,
    "schedule": {"type": "daily", "at": "03:00"}, "params": [],
    "alert_on_failure": True,
    "mutex_group": "reporting", "queue_depth": 0,
    "webhook": {"provider": "github"},
    "run_count": 21, "pinned_count": 1,
    "last_run": {
        "run_id": "r1", "status": "success", "outcome": "success",
        "started_at": "2026-09-22T03:00:01", "duration_seconds": 42.5,
        "trigger": "schedule",
    },
    "stats": {
        "p50": 41.0, "p95": 88.0, "success_rate_30d": 0.93, "completed_count": 30,
        "last7": [{"run_id": "r%d" % i, "status": "success", "outcome": "success"}
                  for i in range(7)],
    },
}

# A running job whose history holds both a live and an unconfirmed dot, so
# their colours can be compared in one place.
_BUSY = {
    "id": "busy", "name": "Photo library backup", "target_kind": "bat",
    "schedule_chip": "daily 01:00", "running": True, "stuck": False,
    "paused": False, "args": "", "params": [],
    "schedule": {"type": "daily", "at": "01:00"},
    "last_run": {"run_id": "r3", "status": "running", "outcome": "running",
                 "started_at": "2026-09-22T01:00:00"},
    "stats": {"last7": [
        {"run_id": "r1", "status": "failed", "outcome": "unconfirmed"},
        {"run_id": "r2", "status": "success", "outcome": "success"},
        {"run_id": "r3", "status": "running", "outcome": "running"},
    ]},
}


def _open_jobs(page: Page, base_url: str) -> None:
    now = int(time.time())
    jobs = [dict(_JOB, next_run_epoch=now + 6 * 3600),
            dict(_BUSY, next_run_epoch=now + 12 * 3600)]
    page.route(
        re.compile(r".*/api/jobs(\?.*)?$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps({"jobs": jobs})),
    )
    page.route(
        re.compile(r".*/api/jobs/[^/]+/runs$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps({"runs": []})),
    )
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabJobs").click()
    expect(page.locator("#jobsList li.job-row[data-id='demo']")).to_be_visible()


# The three parts of the row's main button, measured: the avatar leads, the
# text column follows, the history trails — and the kebab sits after the main
# button. Read in one stable_eval so a poll's re-render cannot split the reads.
_LAYOUT = """
(li) => {
  const r = (el) => el.getBoundingClientRect();
  const main = li.querySelector('.action-row-main');
  const av = li.querySelector('.job-avatar');
  const text = li.querySelector('.action-row-text');
  const spark = li.querySelector('.job-sparkline');
  const kebab = li.querySelector('.action-row-kebab');
  const meta = li.querySelector('.action-row-meta');
  return {
    row: Math.round(r(li).height),
    avatarBeforeText: r(av).right <= r(text).left + 1,
    sparkAfterText: r(spark).left >= r(text).right - 1,
    kebabAfterMain: r(kebab).left >= r(main).right - 1,
    kebab: [Math.round(r(kebab).width), Math.round(r(kebab).height)],
    metaHeight: Math.round(r(meta).height),
    metaLine: parseFloat(getComputedStyle(meta).lineHeight) || 0,
  };
}
"""


def test_row_is_avatar_name_one_meta_line_history_and_a_kebab(
    authed_page: Page, base_url: str
) -> None:
    _open_jobs(authed_page, base_url)
    row = authed_page.locator("#jobsList li.job-row[data-id='demo']")

    m = stable_eval(row, _LAYOUT)
    assert m["row"] == 60, f"job row is {m['row']}px tall at 390px, expected 60"
    assert m["avatarBeforeText"] and m["sparkAfterText"] and m["kebabAfterMain"], m
    assert min(m["kebab"]) >= 36, f"kebab under the touch floor: {m['kebab']}"
    # One meta line: the box is no taller than a line of its own text.
    assert m["metaHeight"] <= max(m["metaLine"], 16) + 2, m

    expect(row.locator(".job-avatar")).to_have_attribute("data-badge", "none")
    expect(row.locator(".action-row-title")).to_have_text("Nightly reconciliation")
    expect(row.locator("[data-role='job-meta']")).to_have_text(
        re.compile(r"^in 6h · Daily 03:00$"))
    expect(row.locator("[data-role='sparkline'] .job-spark-dot")).to_have_count(7)
    # Chips are for exceptions only: a healthy job carries none.
    expect(row.locator(".chip")).to_have_count(0)

    # Gone from the row (#1438): the Run button, bell, status dot, countdown and
    # cadence pills, and the demoted duration / type / mutex / elevated facts.
    for sel in ("[data-role='run-btn']", ".job-run-btn", "[data-role='alert-icon']",
                "[data-role='status-dot']", "[data-role='countdown-chip']",
                "[data-role='cadence-chip']", ".job-row-pills",
                "[data-role='duration-chip']", ".job-mutex-pill", ".job-elevated-pill"):
        expect(row.locator(sel)).to_have_count(0)
    # The row's own controls are the main button and the kebab, nothing else.
    expect(row.locator(":scope > button")).to_have_count(2)
    expect(row.locator(".action-row-kebab")).to_have_attribute(
        "aria-label", "Nightly reconciliation actions")

    # A running job: the avatar's alive badge says so, the meta line leads with
    # it, no chip is raised for the (history) outcome, and the live dot is the
    # accent colour rather than the amber of an unconfirmed one.
    busy = authed_page.locator("#jobsList li.job-row[data-id='busy']")
    expect(busy.locator(".job-avatar")).to_have_attribute("data-badge", "alive")
    expect(busy.locator(".job-avatar .avatar-badge[data-badge='alive']")).to_have_count(1)
    expect(busy.locator("[data-role='job-meta']")).to_have_text(
        re.compile(r"^running now · Daily 01:00$"))
    expect(busy.locator(".chip")).to_have_count(0)
    live = busy.locator(".job-spark-dot.live")
    unconfirmed = busy.locator(".job-spark-dot.unconfirmed")
    expect(live).to_have_count(1)
    colours = [
        stable_read(lambda d=d: d.evaluate("el => getComputedStyle(el).color"))
        for d in (live, unconfirmed)
    ]
    assert colours[0] != colours[1], f"live dot drawn in the unconfirmed amber: {colours}"

    # -- was test_everything_demoted_is_in_the_detail_block (merged in #1215;
    # last: opening the sheet re-renders the list). Since #1438 the detail
    # block sits in the job sheet, not an inline accordion. --
    row.locator(".action-row-main").click()
    expect(authed_page.locator("#jobSheet")).to_be_visible()
    expect(authed_page.locator("#jobSheetTitle")).to_have_text("Nightly reconciliation")
    details = authed_page.locator("#jobSheetBody [data-role='job-details']")
    expect(details).to_be_visible()
    for text in (
        "py",                 # type
        "daily 03:00",        # schedule
        "2026-09-23 03:00",   # next run
        "p50",                # percentiles
        "93% over 30 days",   # success rate
        "21 kept",            # retention
        "1 pinned",
        "success",            # last-run sentence
    ):
        expect(details).to_contain_text(text)
    # The bell left the row: the alert-on-failure fact is the sheet's Alerts line.
    expect(details.locator("[data-role='alerts-line']")).to_contain_text(
        "Telegram, on failure")
    # The situational flags travel with it.
    expect(details.locator(".job-mutex-pill")).to_contain_text("reporting")
    expect(details.locator(".job-webhook-pill")).to_contain_text("github")
    expect(authed_page.locator("#jobSheetRun")).to_have_text("Run now")


def test_the_kebab_holds_run_pause_dry_run_edit_and_remove(
    authed_page: Page, base_url: str
) -> None:
    _open_jobs(authed_page, base_url)
    row = authed_page.locator("#jobsList li.job-row[data-id='demo']")
    row.locator(".action-row-kebab").click()
    labels = [
        "Run Nightly reconciliation now",
        "Pause schedule for Nightly reconciliation",
        "Dry-run check",
        "Edit Nightly reconciliation",
        "Remove Nightly reconciliation",
    ]
    for label in labels:
        expect(row.locator(f".row-menu-btn[aria-label='{label}']")).to_be_visible()
    # Remove is last, the danger item; Edit no longer waits for an Edit mode.
    assert row.locator(".row-menu-btn").evaluate_all(
        "els => els.map((e) => e.getAttribute('aria-label'))") == labels
    expect(row.locator(".job-remove-item")).to_have_class(re.compile(r"\brow-menu-danger\b"))
