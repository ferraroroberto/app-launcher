"""Regression pin for issue #229 (Jobs tab: sort by next execution + countdown).

The Jobs list defaults to **Next run** order — ascending by the server-computed
``next_run_epoch`` — so imminent jobs float to the top and manual-only / paused
jobs (no next fire) sink to the bottom. Each scheduled row carries a relative
countdown chip ("in 3h"); a header toggle flips the order to A–Z.

Hermetic: route-mock ``/api/jobs`` with three fixed jobs whose next-run order
(Zeta, Alpha, Mango) deliberately differs from A–Z (Alpha, Mango, Zeta) so the
two orderings are distinguishable. Runs in both projections.
"""

from __future__ import annotations

import json as _json
import re
import time

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

_NOW = int(time.time())


def _job(name, *, job_id, next_epoch, chip, sched):
    """One decorated /api/jobs row with the fields renderJobRow reads."""
    return {
        "id": job_id,
        "name": name,
        "target_kind": "py",
        "schedule_chip": chip,
        "next_run": None,
        "next_run_epoch": next_epoch,
        "next_run_iso": None,
        "running": False,
        "stuck": False,
        "paused": False,
        "args": "",
        "schedule": sched,
        "params": [],
        "last_run": None,
        "stats": {
            "p50": None, "p95": None, "success_rate_30d": None,
            "completed_count": 0, "last7": [],
        },
    }


def _wire_jobs(page: Page) -> None:
    jobs = [
        # A–Z: Alpha, Mango, Zeta. Next-run: Zeta (+10m), Alpha (+2h), Mango (none).
        _job("Alpha", job_id="alpha", next_epoch=_NOW + 7200,
             chip="daily 12:00", sched={"type": "daily", "at": "12:00"}),
        _job("Mango", job_id="mango", next_epoch=None,
             chip="", sched={"type": "none"}),
        _job("Zeta", job_id="zeta", next_epoch=_NOW + 600,
             chip="daily 06:00", sched={"type": "daily", "at": "06:00"}),
    ]
    page.route(
        re.compile(r".*/api/jobs(\?.*)?$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"jobs": jobs}),
        ),
    )


def _row_ids(page: Page):
    return page.eval_on_selector_all(
        "#jobsList li.app-item[data-id]",
        "els => els.map(e => e.dataset.id)",
    )


def test_jobs_default_to_next_run_order_with_countdown(
    authed_page: Page, base_url: str
) -> None:
    # Guard against a sort pref leaking from a reused context — default is 'next'.
    authed_page.add_init_script(
        "() => localStorage.removeItem('launcher.jobsSort')"
    )
    _wire_jobs(authed_page)

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabJobs").click()
    authed_page.wait_for_selector(
        "#jobsList li.app-item[data-id]", state="attached", timeout=5_000
    )

    # Default order is by next fire: Zeta (+10m), Alpha (+2h), Mango (none, last).
    assert _row_ids(authed_page) == ["zeta", "alpha", "mango"], (
        "default sort should be ascending by next_run_epoch, nulls last"
    )

    # The imminent job shows a countdown chip; the manual job shows none.
    zeta_chip = authed_page.locator(
        "#jobsList li[data-id='zeta'] [data-role='countdown-chip']"
    )
    expect(zeta_chip).to_contain_text("in")
    assert authed_page.locator(
        "#jobsList li[data-id='mango'] [data-role='countdown-chip']"
    ).count() == 0, "a job with no next fire must not show a countdown chip"


def test_sort_toggle_switches_to_alphabetical(
    authed_page: Page, base_url: str
) -> None:
    authed_page.add_init_script(
        "() => localStorage.removeItem('launcher.jobsSort')"
    )
    _wire_jobs(authed_page)

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabJobs").click()
    authed_page.wait_for_selector(
        "#jobsList li.app-item[data-id]", state="attached", timeout=5_000
    )
    assert _row_ids(authed_page) == ["zeta", "alpha", "mango"]

    # Toggle → A–Z. The button lives in the summary; the click must flip the
    # sort without collapsing the <details>.
    authed_page.locator("#jobsSortBtn").click()
    assert _is_open(authed_page), "sort toggle must not collapse the jobs panel"
    expect(authed_page.locator("#jobsList li.app-item[data-id]").first).to_have_attribute(
        "data-id", "alpha"
    )
    assert _row_ids(authed_page) == ["alpha", "mango", "zeta"], (
        "A–Z order should be name-sorted regardless of next fire"
    )


def _is_open(page: Page) -> bool:
    return bool(
        page.locator("#paneJobs details.jobs-card").evaluate("el => el.open")
    )
