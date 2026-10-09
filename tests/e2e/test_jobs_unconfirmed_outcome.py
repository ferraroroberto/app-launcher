"""Regression pin for issue #916 (Jobs: exit 122 means "not confirmed").

The Jobs surface rendered every non-zero exit as ``failed``. Exit 122 is
fleet-config's scheduled-run adapter saying the completion stream was
truncated — the run may well have delivered, but nobody established that — so
four healthy weekly jobs showed red for it, and the genuinely failed ones
beside them (exit 123, "the run reported it delivered no work") became
indistinguishable from the noise.

These pin the render, which is the half a Python test cannot reach: the
unconfirmed row must be tellable apart from **both** neighbours, not merely
different from one of them.

Issue #1316 added a fourth terminal outcome, ``deferred``: an exit code the
job itself declares a designed deferral (life-os's email sweep exits 3 when it
has no desktop to start Outlook on). It rides on the same render, because it
also has to be tellable apart from all three of the others.

#1438 rebuilt the row: the coloured status dot is gone. The same classification
now reads as an exception chip on the row's meta line ("failed" danger, "not
confirmed" attention, none for a clean or deferred job) and as the history
sparkline's dot class, and the header counts only genuine failures. These pins
moved onto those; the exit code's one-liner rides on the chip's tooltip, and
the last-run sentence is in the job sheet.

Hermetic: route-mock ``/api/jobs`` and one job's run history with four fixed
rows — exit 0, exit 122, exit 123, a declared exit 3 — so nothing here depends
on real run history or on a job ever having failed. Runs in both projections.
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import stable_read

pytestmark = pytest.mark.smoke


def _job(name, *, job_id, last_run, last7):
    """One decorated /api/jobs row with the fields renderJobRow reads."""
    return {
        "id": job_id,
        "name": name,
        "target_kind": "py",
        "schedule_chip": "weekly",
        "next_run": None,
        "next_run_epoch": None,
        "next_run_iso": None,
        "running": False,
        "stuck": False,
        "paused": False,
        "elevated": False,
        "manual_run_allowed": True,
        "schedule_controls_allowed": True,
        "args": "",
        "schedule": {"type": "none"},
        "params": [],
        "last_run": last_run,
        "stats": {
            "p50": None, "p95": None, "success_rate_30d": None,
            "unconfirmed_30d": 0, "completed_count": 0, "last7": last7,
        },
    }


def _last_run(*, status, outcome, exit_code, reason):
    return {
        "run_id": "20260911T002001",
        "status": status,
        "outcome": outcome,
        "outcome_reason": reason,
        "started_at": "2026-09-11T00:20:01",
        "finished_at": "2026-09-11T00:27:08",
        "exit_code": exit_code,
        "trigger": "scheduled",
        "duration_seconds": 426.9,
    }


# The three terminal outcomes, one job each: a clean run, the run this issue is
# about, and a run that genuinely failed and must keep saying so.
_JOBS = [
    _job("Clean", job_id="clean", last7=[{"status": "success", "outcome": "success", "run_id": "r0"}],
         last_run=_last_run(status="success", outcome="success", exit_code=0, reason=None)),
    _job("Truncated", job_id="truncated",
         last7=[{"status": "failed", "outcome": "unconfirmed", "run_id": "r1"}],
         last_run=_last_run(
             status="failed", outcome="unconfirmed", exit_code=122,
             reason="the completion stream was truncated — the run was cut off "
                    "mid-flight and delivery was never verified",
         )),
    _job("Delivered nothing", job_id="broken",
         last7=[{"status": "failed", "outcome": "failed", "run_id": "r2"}],
         last_run=_last_run(
             status="failed", outcome="failed", exit_code=123,
             reason="the run reported it delivered no work",
         )),
    _job("Deferred sweep", job_id="deferred",
         last7=[{"status": "failed", "outcome": "deferred", "run_id": "r3"}],
         last_run=_last_run(
             status="failed", outcome="deferred", exit_code=3,
             reason="exit 3 is declared deferred by this job",
         )),
]


def _wire(page: Page) -> None:
    page.route(
        re.compile(r".*/api/jobs(\?.*)?$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"jobs": _JOBS}),
        ),
    )


def _open_jobs(page: Page, base_url: str) -> None:
    _wire(page)
    page.route(
        re.compile(r".*/api/jobs/[^/]+/runs$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps({"runs": []})),
    )
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabJobs").click()
    page.wait_for_selector(
        "#jobsList li.job-row[data-id]", state="attached", timeout=5_000
    )


def _row(page: Page, job_id: str):
    return page.locator(f"#jobsList li.job-row[data-id='{job_id}']")


def _first_dot(page: Page, job_id: str):
    return _row(page, job_id).locator("[data-role='sparkline'] .job-spark-dot").first


# What a history dot looks like, as rendered: the deferred ring differs from
# the muted `unknown` dot only by its transparent fill and ring, so the
# signature is fill + ring + colour rather than a colour alone.
_DOT_LOOK = """el => { const s = getComputedStyle(el);
  return [s.backgroundColor, s.boxShadow, s.color].join('|'); }"""


@pytest.mark.iphone
def test_terminal_outcomes_render_distinctly(
    authed_page: Page, base_url: str
) -> None:
    """All four terminal outcomes on one render of the Jobs list (#916, #1316).

    One page load for every check (#1215): the chip and dot-class pins, the
    pairwise look fact, the header count, and last the job sheet the
    unconfirmed row opens.
    """
    _open_jobs(authed_page, base_url)

    # -- was test_unconfirmed_row_is_not_rendered_as_failed --
    # The defect itself: exit 122 got the same red as a real failure. Its row
    # says "not confirmed" (attention), and carries no failure chip.
    unconfirmed = _row(authed_page, "truncated").locator(".job-unconfirmed-chip")
    expect(unconfirmed).to_have_text("not confirmed")
    expect(unconfirmed).to_have_attribute("data-tone", "attention")
    expect(_row(authed_page, "truncated").locator(".job-failed-chip")).to_have_count(0)

    # -- was test_genuine_failure_still_renders_as_failed --
    # The over-correction guard. Exit 123 is the run reporting it delivered
    # no work; replacing false alarms with false comfort would be worse than the
    # bug. A clean run raises no chip at all.
    failed = _row(authed_page, "broken").locator(".job-failed-chip")
    expect(failed).to_have_text("failed")
    expect(failed).to_have_attribute("data-tone", "danger")
    expect(_row(authed_page, "broken").locator(".job-unconfirmed-chip")).to_have_count(0)
    expect(_row(authed_page, "clean").locator(".chip")).to_have_count(0)
    # The header counts the genuine failure only: not the unconfirmed run, not
    # the deferral, not the clean one (#1438).
    expect(
        authed_page.locator("#jobsHeadStatus .head-exception[data-tone='danger']")
    ).to_have_text("1 failing")

    # -- was test_sparkline_dot_follows_the_outcome_not_the_status --
    # The 7-run sparkline reads the same classification, so a healthy job's
    # history does not show a wall of red beside a corrected row.
    expect(_first_dot(authed_page, "truncated")).to_have_class(re.compile(r"\bunconfirmed\b"))
    expect(_first_dot(authed_page, "broken")).to_have_class(re.compile(r"\bdown\b"))
    expect(_first_dot(authed_page, "clean")).to_have_class(re.compile(r"\bup\b"))

    # #1316: a declared deferral is its own state on the sparkline, neither the
    # success class nor the failure one, and raises no chip (it needs nobody).
    expect(_first_dot(authed_page, "deferred")).to_have_class(re.compile(r"\bdeferred\b"))
    expect(_first_dot(authed_page, "deferred")).not_to_have_class(re.compile(r"\b(down|up)\b"))
    expect(_row(authed_page, "deferred").locator(".chip")).to_have_count(0)

    # -- was test_three_outcomes_are_three_distinct_colours --
    # "Visually distinct from both success and failure" — asserted as the
    # pairwise fact rather than against hardcoded token values, so a future
    # palette change cannot make this pass while the states look alike. (The
    # row's status dot is gone; the history dot and the two chips carry it.)
    looks = {}
    for job_id in ("clean", "truncated", "broken", "deferred"):
        expect(_first_dot(authed_page, job_id)).to_be_visible()
        looks[job_id] = stable_read(
            lambda jid=job_id: _first_dot(authed_page, jid).evaluate(_DOT_LOOK))
    assert all(looks.values()), f"unreadable looks: {looks}"
    assert len(set(looks.values())) == 4, (
        "success, unconfirmed, failed and deferred must each look different; "
        f"got {looks}"
    )
    chip_colours = [
        stable_read(lambda c=c: c.evaluate("el => getComputedStyle(el).color"))
        for c in (unconfirmed, failed)
    ]
    assert chip_colours[0] != chip_colours[1], (
        f"'not confirmed' and 'failed' chips share a colour: {chip_colours}")

    # -- was test_unconfirmed_row_says_not_confirmed_and_explains_itself --
    # (last: it opens the job sheet.)
    # The word on the row matters as much as the colour: "failed" is the
    # claim that was wrong. The exit code's own one-liner rides on the chip's
    # tooltip so the row is actionable without opening the log.
    expect(unconfirmed).to_have_attribute("title", re.compile("never verified"))
    # The last-run sentence is in the job sheet's detail block (#1130, #1438).
    _row(authed_page, "truncated").locator(".action-row-main").click()
    meta = authed_page.locator("#jobSheetBody [data-role='job-details']")
    expect(meta).to_contain_text("not confirmed")
    expect(meta).not_to_contain_text("failed")
