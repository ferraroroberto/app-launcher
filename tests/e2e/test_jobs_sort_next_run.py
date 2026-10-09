"""Regression pin for issue #229 (Jobs tab: sort by next execution + countdown).

The Jobs list defaults to **Next run** order — ascending by the server-computed
``next_run_epoch`` — so imminent jobs float to the top and manual-only / paused
jobs (no next fire) sink to the bottom. Each scheduled row's meta line leads
with a relative countdown ("in 3h · Daily 12:00"; it was a "Next in" pill
before #1438); a toolbar icon button flips the order to A–Z.

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

from tests.e2e.conftest import stable_read

pytestmark = pytest.mark.smoke


def _job(name, *, job_id, next_epoch, chip, sched, elevated=False):
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
        "elevated": elevated,
        "manual_run_allowed": not elevated,
        "schedule_controls_allowed": not elevated,
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
    # Compute at call time, not module/collection time (#646) — a full
    # dual-projection run can take ~10+ minutes between collection and this
    # test's turn, which would otherwise burn through Zeta's margin before
    # the assertion runs. Never hoist this back to module scope.
    now = int(time.time())
    jobs = [
        # A–Z: Alpha, Mango, Zeta. Next-run: Zeta (+10m), Alpha (+2h), Mango (none).
        _job("Alpha", job_id="alpha", next_epoch=now + 7200,
             chip="daily 12:00", sched={"type": "daily", "at": "12:00"}),
        _job("Mango", job_id="mango", next_epoch=None,
             chip="", sched={"type": "none"}),
        _job("Zeta", job_id="zeta", next_epoch=now + 600,
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
        "#jobsList li.job-row[data-id]",
        "els => els.map(e => e.dataset.id)",
    )


@pytest.mark.iphone
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
        "#jobsList li.job-row[data-id]", state="attached", timeout=5_000
    )

    # Default order is by next fire: Zeta (+10m), Alpha (+2h), Mango (none, last).
    assert _row_ids(authed_page) == ["zeta", "alpha", "mango"], (
        "default sort should be ascending by next_run_epoch, nulls last"
    )

    # The imminent job's meta line leads with a countdown; the manual job's
    # says "Manual only" and has none (the pill became the meta line, #1438).
    expect(authed_page.locator(
        "#jobsList li[data-id='zeta'] [data-role='job-meta']"
    )).to_have_text("in 10m · Daily 06:00")
    expect(authed_page.locator(
        "#jobsList li[data-id='alpha'] [data-role='job-meta']"
    )).to_have_text("in 2h · Daily 12:00")
    expect(authed_page.locator(
        "#jobsList li[data-id='mango'] [data-role='job-meta']"
    )).to_have_text("Manual only")

    # #1207, kept in spirit: a schedule-less job keeps its kebab (Run now is
    # its first item) so the kebab column lines up with a scheduled row's; only
    # Pause, which has nothing to pause, is left out of its menu (#1438).
    mango = authed_page.locator("#jobsList li[data-id='mango']")
    xs = [
        stable_read(lambda i=i: authed_page.locator(
            f"#jobsList li[data-id='{i}'] .action-row-kebab").bounding_box())
        for i in ("zeta", "mango")
    ]
    assert all(xs), f"kebabs not laid out: {xs}"
    assert abs(xs[0]["x"] - xs[1]["x"]) <= 1, f"kebab jumps sideways on a manual job: {xs}"
    mango.locator(".action-row-kebab").click()
    expect(mango.locator(".job-run-item")).to_be_visible()
    expect(mango.locator(".job-pause-item")).to_have_count(0)
    authed_page.keyboard.press("Escape")

    # -- was test_sort_toggle_switches_to_alphabetical (merged in #1215; last:
    # it flips the persisted sort pref) --
    assert _row_ids(authed_page) == ["zeta", "alpha", "mango"]

    # Toggle → A–Z. The icon button sits in the card's toolbar (not the
    # summary); the click must flip the sort without collapsing the <details>.
    sort_btn = authed_page.locator("#jobsSortBtn")
    expect(sort_btn).to_have_attribute("data-sort", "next")
    expect(sort_btn).to_have_attribute("aria-label", re.compile("Sorted by next run"))
    sort_btn.click()
    assert _is_open(authed_page), "sort toggle must not collapse the jobs panel"
    expect(sort_btn).to_have_attribute("data-sort", "name")
    expect(sort_btn).to_have_attribute("aria-label", re.compile("Sorted A–Z"))
    expect(authed_page.locator("#jobsList li.job-row[data-id]").first).to_have_attribute(
        "data-id", "alpha"
    )
    assert _row_ids(authed_page) == ["alpha", "mango", "zeta"], (
        "A–Z order should be name-sorted regardless of next fire"
    )


def test_external_schedule_card_only_offers_history(
    authed_page: Page, base_url: str
) -> None:
    # Computed at call time (#646) — see _wire_jobs for why.
    external = _job(
        "HWiNFO restart",
        job_id="hwinfo-restart",
        next_epoch=int(time.time()) + 3600,
        chip="every 8 h",
        sched={"type": "hourly", "every": 8},
        elevated=True,
    )
    authed_page.route(
        re.compile(r".*/api/jobs(\?.*)?$"),
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=_json.dumps({"jobs": [external]}),
        ),
    )

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabJobs").click()
    row = authed_page.locator("#jobsList li[data-id='hwinfo-restart']")
    # The external-schedule flag lives in the job sheet's detail block (#1130,
    # #1438). The row no longer withholds Run / the menu outright (it has no
    # Run button at all): the kebab is always there, but its Run now is
    # disabled with the reason and Pause is left out, since every control an
    # externally managed schedule cannot take is withheld.
    row.locator(".action-row-kebab").click()
    run_item = row.locator(".job-run-item")
    expect(run_item).to_be_disabled()
    expect(run_item).to_have_attribute("title", re.compile("externally managed"))
    expect(row.locator(".job-pause-item")).to_have_count(0)
    authed_page.keyboard.press("Escape")
    row.locator(".action-row-main").click()
    details = authed_page.locator("#jobSheetBody [data-role='job-details']")
    expect(details.locator(".job-elevated-pill")).to_contain_text(
        "external schedule"
    )
    expect(authed_page.locator("#jobSheetRun")).to_be_disabled()


def test_manual_run_state_is_distinct_from_next_scheduled_fire(
    authed_page: Page, base_url: str
) -> None:
    # Computed at call time (#646) — see _wire_jobs for why.
    running = _job(
        "LI scrape",
        job_id="linkedin-scrape",
        next_epoch=int(time.time()) + 14 * 60,
        chip="daily 06:15 12:00 18:00",
        sched={"type": "daily_times", "at": ["06:15", "12:00", "18:00"]},
    )
    running["running"] = True
    running["next_run"] = "2026-07-16 06:15"
    running["last_run"] = {
        "run_id": "20260716T060156",
        "status": "running",
        "started_at": "2026-07-16T06:01:57",
        "trigger": "manual",
        "duration_seconds": None,
    }
    authed_page.route(
        re.compile(r".*/api/jobs(\?.*)?$"),
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=_json.dumps({"jobs": [running]}),
        ),
    )

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabJobs").click()
    row = authed_page.locator("#jobsList li[data-id='linkedin-scrape']")
    # Since #1438 the row's meta line says "running now" in place of the
    # countdown (the avatar's alive badge says it too); the *next scheduled
    # fire* stays a separate fact, the sheet's "Next run" row, beside the
    # "running now" last-run sentence (#1130).
    expect(row.locator("[data-role='job-meta']")).to_have_text(
        re.compile(r"^running now · Daily 06:15 12:00 18:00$"))
    expect(row.locator(".job-avatar")).to_have_attribute("data-badge", "alive")
    row.locator(".action-row-main").click()
    details = authed_page.locator("#jobSheetBody [data-role='job-details']")
    expect(details.locator(".job-detail-row", has_text="Last run")).to_contain_text(
        "running now")
    expect(details.locator(".job-detail-row", has_text="Next run")).to_contain_text(
        "06:15")


def _is_open(page: Page) -> bool:
    return bool(
        page.locator("#paneJobs details.jobs-card").evaluate("el => el.open")
    )
