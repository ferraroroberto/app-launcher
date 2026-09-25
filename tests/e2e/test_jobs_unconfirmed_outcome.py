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

Hermetic: route-mock ``/api/jobs`` and one job's run history with three fixed
rows — exit 0, exit 122, exit 123 — so nothing here depends on real run
history or on a job ever having failed. Runs in both projections.
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
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabJobs").click()
    page.wait_for_selector(
        "#jobsList li.app-item[data-id]", state="attached", timeout=5_000
    )


def _dot(page: Page, job_id: str):
    return page.locator(f"#jobsList li[data-id='{job_id}'] [data-role='status-dot']")


def test_three_outcomes_render_distinctly(
    authed_page: Page, base_url: str
) -> None:
    """All three terminal outcomes on one render of the Jobs list (#916).

    One page load for every check (#1215): the dot-class pins, the pairwise
    colour fact, and last the detail block the unconfirmed row opens.
    """
    _open_jobs(authed_page, base_url)

    # -- was test_unconfirmed_row_is_not_rendered_as_failed --
    # The defect itself: exit 122 got the same red dot as a real failure.
    expect(_dot(authed_page, "truncated")).to_have_class(
        re.compile(r"\bunconfirmed\b")
    )
    # And specifically *not* the failure class it used to get.
    expect(_dot(authed_page, "truncated")).not_to_have_class(re.compile(r"\bdown\b"))

    # -- was test_genuine_failure_still_renders_as_failed --
    # The over-correction guard. Exit 123 is the run reporting it delivered
    # no work; replacing false alarms with false comfort would be worse than the
    # bug.
    expect(_dot(authed_page, "broken")).to_have_class(re.compile(r"\bdown\b"))
    expect(_dot(authed_page, "clean")).to_have_class(re.compile(r"\bup\b"))

    # -- was test_sparkline_dot_follows_the_outcome_not_the_status --
    # The 7-run sparkline reads the same classification, so a healthy job's
    # history does not show a wall of red beside a corrected row.
    spark = authed_page.locator(
        "#jobsList li[data-id='truncated'] [data-role='sparkline'] .job-spark-dot"
    ).first
    expect(spark).to_have_class(re.compile(r"\bunconfirmed\b"))
    expect(
        authed_page.locator(
            "#jobsList li[data-id='broken'] [data-role='sparkline'] .job-spark-dot"
        ).first
    ).to_have_class(re.compile(r"\bdown\b"))

    # -- was test_three_outcomes_are_three_distinct_colours --
    # "Visually distinct from both success and failure" — asserted as the
    # pairwise fact rather than against hardcoded token values, so a future
    # palette change cannot make this pass while the states look alike.
    colours = {}
    for job_id in ("clean", "truncated", "broken"):
        expect(_dot(authed_page, job_id)).to_be_visible()
        colours[job_id] = stable_read(
            lambda jid=job_id: _dot(authed_page, jid).evaluate(
                "el => getComputedStyle(el).backgroundColor"
            )
        )
    assert all(colours.values()), f"unreadable colours: {colours}"
    assert len(set(colours.values())) == 3, (
        "success, unconfirmed and failed must each have their own colour; "
        f"got {colours}"
    )

    # -- was test_unconfirmed_row_says_not_confirmed_and_explains_itself --
    # (last: it opens the row's detail block.)
    # The word on the row matters as much as the colour: "failed" is the
    # claim that was wrong. The exit code's own one-liner rides on the dot's
    # tooltip so the row is actionable without opening the log.
    # The last-run sentence moved into the detail block the row opens (#1130).
    authed_page.locator(
        "#jobsList li[data-id='truncated'] button[aria-label^='View run history']"
    ).click()
    meta = authed_page.locator("[data-role='job-details']")
    expect(meta).to_contain_text("not confirmed")
    expect(meta).not_to_contain_text("failed")
    expect(_dot(authed_page, "truncated")).to_have_attribute(
        "title", re.compile("never verified")
    )
