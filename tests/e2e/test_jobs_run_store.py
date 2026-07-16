"""Jobs run-store UI regression coverage for issue #71."""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke


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
    row = authed_page.locator("#jobsList li.app-item[data-id='demo']")
    expect(row).to_be_visible()
    expect(row.locator(".meta")).to_contain_text("21 kept")
    expect(row.locator(".meta")).to_contain_text("1 pinned")

    row.locator(".launch-btn").click()
    expect(authed_page.locator(".jobs-artifacts")).to_be_visible()
    artifact = authed_page.locator(".jobs-artifacts a")
    expect(artifact).to_have_text(re.compile(r"report\.csv"))
    assert "/artifacts/report.csv" in (artifact.get_attribute("href") or "")

    authed_page.locator("#jobsSearchInput").fill("unique-needle")
    hit = authed_page.locator(".job-search-hit")
    expect(hit).to_be_visible()
    expect(hit).to_contain_text("unique-needle")
    hit.locator(".launch-btn").click()
    expect(authed_page.locator(".jobs-output-tail")).to_contain_text("report complete")

    pin = authed_page.locator(".jobs-pin-btn")
    expect(pin).to_have_attribute("aria-pressed", "true")
    pin.click()
    expect(pin).to_have_attribute("aria-pressed", "false")
    assert pin_payloads == [{"pinned": False}]

