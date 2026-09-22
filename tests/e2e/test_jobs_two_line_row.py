"""Regression pin for #1130 — the job row is two lines with one action.

A row used to carry about fourteen data points across five lines (status dot,
name, bell, type chip, cadence, countdown, p50, p95, a seven-dot history,
last result, age, duration, success rate and retention) plus a stacked rail
of five equally-weighted glyphs. Nothing read first, and the opened Jobs pane
ran to 6,386px on an iPhone.

The row now reads: **line 1** status dot + name (+ bell), **line 2** when it
next runs, how often, the seven-dot history — plus the not-firing alert when
there is one, which is an alert rather than a detail. Its one visible action
is Run at the tint tier and the 44px touch floor; Pause/Resume, the dry-run
check, Edit and Remove are in the row's ⋯ menu.

Everything demoted is reachable in the detail block the row opens, which is
the other half of the contract: this asserts both, so a later trim cannot
quietly drop a fact instead of moving it.
"""
from __future__ import annotations

import json as _json
import re
import time

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

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


def _open_jobs(page: Page, base_url: str, *, edit_mode: bool = False) -> None:
    job = dict(_JOB)
    job["next_run_epoch"] = int(time.time()) + 6 * 3600
    page.add_init_script(
        "localStorage.setItem('launcher.editMode', '%s')" % ("1" if edit_mode else "0")
    )
    page.route(
        re.compile(r".*/api/jobs(\?.*)?$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"jobs": [job]})),
    )
    page.route(
        re.compile(r".*/api/jobs/demo/runs$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps({"runs": []})),
    )
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabJobs").click()
    expect(page.locator("#jobsList li.app-item[data-id='demo']")).to_be_visible()


# Two lines, measured rather than inferred from which elements exist: the
# head is one line, and line 2 must not *wrap*. A container-only count would
# miss exactly that — the pills container wraps internally, so its chips can
# spill onto a third visual row while the element count still reads two.
_LINES = """
() => {
  const row = document.querySelector("#jobsList li[data-id='demo']");
  const info = row && row.querySelector('.launch-btn');
  const pills = row && row.querySelector('.job-row-pills');
  if (!info || !pills) return null;
  const kids = Array.from(pills.children).map((el) => el.getBoundingClientRect());
  const tallest = Math.max.apply(null, kids.map((r) => r.height));
  const span = Math.max.apply(null, kids.map((r) => r.bottom))
    - Math.min.apply(null, kids.map((r) => r.top));
  return {
    // > 1 when line 2 wrapped onto another row of chips.
    pillRows: Math.round(span / Math.max(tallest, 1) * 10) / 10,
    parts: kids.length,
    height: Math.round(info.getBoundingClientRect().height),
  };
}
"""


def test_row_is_two_lines_with_one_action_and_a_menu(
    authed_page: Page, base_url: str
) -> None:
    _open_jobs(authed_page, base_url)
    row = authed_page.locator("#jobsList li.app-item[data-id='demo']")

    m = authed_page.evaluate(_LINES)
    assert m is not None, "job row not rendered"
    assert m["parts"] == 3, f"line 2 should hold next/cadence/history, got {m['parts']}"
    assert m["pillRows"] <= 1.2, (
        f"line 2 wrapped at 390px (its chips span {m['pillRows']}x their own "
        "height) — the row is meant to be two lines"
    )
    assert m["height"] <= 78, f"the row's text column is {m['height']}px tall"

    # Line 1: dot, name, bell.
    expect(row.locator("[data-role='status-dot']")).to_have_count(1)
    expect(row.locator(".job-row-head .name")).to_have_text("Nightly reconciliation")
    expect(row.locator("[data-role='alert-icon']")).to_have_count(1)
    # Line 2: next run, cadence in sentence case, the seven-dot history.
    expect(row.locator("[data-role='countdown-chip']")).to_contain_text("Next in 6h")
    expect(row.locator("[data-role='cadence-chip']")).to_have_text("Daily 03:00")
    expect(row.locator("[data-role='sparkline'] .job-spark-dot")).to_have_count(7)
    # Values are not shouted at (the uppercase transform is gone).
    expect(row.locator("[data-role='cadence-chip']")).to_have_css(
        "text-transform", "none")

    # Demoted from the row.
    for role in ("duration-chip", "meta", "elevated-chip", "mutex-pill"):
        expect(row.locator(f"[data-role='{role}']")).to_have_count(0)

    # One visible action, at the touch floor, plus the ⋯ menu.
    run = row.locator("[data-role='run-btn']")
    expect(run).to_be_visible()
    assert run.bounding_box()["height"] >= 44, run.bounding_box()
    expect(run).to_have_class(re.compile(r"\bbutton-tint\b"))
    expect(row.locator("[data-role='job-menu']")).to_have_count(1)
    # Nothing else is a visible action on the row itself.
    expect(row.locator(".row-actions > button")).to_have_count(2)


def test_everything_demoted_is_in_the_detail_block(
    authed_page: Page, base_url: str
) -> None:
    _open_jobs(authed_page, base_url)
    authed_page.locator(
        "#jobsList li[data-id='demo'] button[aria-label^='View run history']"
    ).click()
    details = authed_page.locator("[data-role='job-details']")
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
    # The situational flags travel with it.
    expect(details.locator("[data-role='status-dot']")).to_have_count(0)
    expect(details.locator(".job-mutex-pill")).to_contain_text("reporting")
    expect(details.locator(".job-webhook-pill")).to_contain_text("github")


def test_the_menu_holds_the_actions_the_rail_used_to_stack(
    authed_page: Page, base_url: str
) -> None:
    _open_jobs(authed_page, base_url, edit_mode=True)
    row = authed_page.locator("#jobsList li.app-item[data-id='demo']")
    row.locator("[data-role='job-menu']").click()
    # Pause names the job it would pause, the way the rail button did.
    expect(row.locator("button[aria-label^='Pause schedule for']")).to_be_visible()
    for label in ("Dry-run check", "Edit", "Remove"):
        expect(row.locator(f"button[aria-label='{label}']")).to_be_visible()
