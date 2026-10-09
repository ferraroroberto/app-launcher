"""Jobs run-store UI regression coverage for issue #71.

Since #1438 the run store (retention counts, artifacts, the run list and the
pin toggle) lives in the job sheet (``dialog#jobSheet`` / ``#jobSheetBody``)
opened from a job row, and a run-output search hit opens that sheet ON the
hit's run instead of expanding an inline accordion.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

# A design token resolved to the computed `rgb(...)` a colour assertion
# compares against, in whichever theme the page is in.
_TOKEN_COLOR = """(name) => {
  const probe = document.createElement('span');
  probe.style.color = 'var(' + name + ')';
  document.body.appendChild(probe);
  const color = getComputedStyle(probe).color;
  probe.remove();
  return color;
}"""


@pytest.mark.iphone
def test_search_jump_artifact_link_and_pin_toggle(
    authed_page: Page, base_url: str
) -> None:
    fake_job = {
        "id": "demo",
        "name": "Demo artifact job",
        "target_kind": "python",
        "schedule": {"type": "none"},
        "schedule_chip": "",
        "next_run": None,
        "next_run_epoch": None,
        "running": False,
        "stuck": False,
        "queue_depth": 0,
        "run_count": 21,
        "pinned_count": 1,
        "last_run": {
            "run_id": "r1",
            "status": "success",
            "started_at": "2026-07-16T09:00:00",
            "duration_seconds": 2.5,
        },
        "stats": {
            "p50": 2.5,
            "p95": 3.0,
            "success_rate_30d": 1.0,
            "completed_count": 21,
            "last7": [{"run_id": "r1", "status": "success"}],
        },
        "params": [],
    }
    fake_run = {
        "run_id": "r1",
        "status": "success",
        "started_at": "2026-07-16T09:00:00",
        "trigger": "manual",
        "exit_code": 0,
        "pinned": True,
    }
    detail = {
        "run": {
            **fake_run,
            "duration_seconds": 2.5,
            "output_tail": "unique-needle\nreport complete\n",
            "artifacts": [
                {"name": "report.csv", "size": 42, "mtime": "2026-07-16T09:00:03"}
            ],
            "webhook_payload": None,
        }
    }
    pin_payloads: list[dict] = []

    authed_page.route(
        re.compile(r".*/api/jobs(\?.*)?$"),
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"jobs": [fake_job]}),
        ),
    )
    authed_page.route(
        re.compile(r".*/api/jobs/runs/search.*"),
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "matches": [
                        {
                            "job_id": "demo",
                            "run_id": "r1",
                            "status": "success",
                            "started_at": "2026-07-16T09:00:00",
                            "snippet": "unique-needle report complete",
                        }
                    ]
                }
            ),
        ),
    )
    authed_page.route(
        re.compile(r".*/api/jobs/demo/runs$"),
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"runs": [fake_run]}),
        ),
    )

    def _run_detail(route) -> None:
        if route.request.method == "PUT":
            payload = route.request.post_data_json
            pin_payloads.append(payload)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"run": {**fake_run, **payload}}),
            )
            return
        route.fulfill(
            status=200, content_type="application/json", body=json.dumps(detail)
        )

    authed_page.route(re.compile(r".*/api/jobs/demo/runs/r1$"), _run_detail)

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.locator("#tabJobs").click()
    row = authed_page.locator("#jobsList li.job-row[data-id='demo']")
    expect(row).to_be_visible()
    # Retention counts live in the detail block of the job sheet (#1130, #1438).
    row.locator("button.action-row-main").click()
    sheet = authed_page.locator("#jobSheet")
    expect(sheet).to_be_visible()
    details = sheet.locator("[data-role='job-details']")
    expect(details).to_contain_text("21 kept")
    expect(details).to_contain_text("1 pinned")

    expect(sheet.locator(".jobs-artifacts")).to_be_visible()
    artifact = sheet.locator(".jobs-artifacts a")
    expect(artifact).to_have_text(re.compile(r"report\.csv"))
    assert "/artifacts/report.csv" in (artifact.get_attribute("href") or "")

    # Close the sheet (it is modal on a phone) and search from the list.
    authed_page.locator("#jobSheetClose").click()
    expect(sheet).not_to_be_visible()
    authed_page.locator("#jobsSearchInput").fill("unique-needle")
    hit = authed_page.locator("#jobsList .job-search-hit")
    expect(hit).to_be_visible()
    expect(hit).to_contain_text("unique-needle")
    hit.locator("button.action-row-main").click()
    # The hit opens the sheet on that run: its output shows and it is selected.
    expect(sheet).to_be_visible()
    expect(sheet.locator(".jobs-output-tail")).to_contain_text("report complete")
    expect(sheet.locator(".jobs-run-btn.selected")).to_have_count(1)

    pin = sheet.locator(".jobs-pin-btn")
    expect(pin).to_have_attribute("aria-pressed", "true")
    # #1333: the pin is a quiet glyph, never a tile: no fill and no border
    # in either state, the accent glyph when pinned, the muted one when not.
    accent = authed_page.evaluate(_TOKEN_COLOR, "--accent")
    muted = authed_page.evaluate(_TOKEN_COLOR, "--muted")
    expect(pin).to_have_css("background-color", "rgba(0, 0, 0, 0)")
    expect(pin).to_have_css("border-top-style", "none")
    expect(pin).to_have_css("filter", "none")
    expect(pin).to_have_css("color", accent)
    pin.click()
    expect(pin).to_have_attribute("aria-pressed", "false")
    authed_page.mouse.move(0, 0)  # off the pin: its hover state brightens it
    expect(pin).to_have_css("background-color", "rgba(0, 0, 0, 0)")
    expect(pin).to_have_css("color", muted)
    assert pin_payloads == [{"pinned": False}]

