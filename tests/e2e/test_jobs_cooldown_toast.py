"""Regression pin for issue #403 (cooldown-blocked run misreported as failed).

FastAPI double-wraps ``raise HTTPException(detail={...})`` in its own
``{"detail": ...}`` envelope, so the cooldown payload is nested one level
deeper than jobs.js originally expected — the ``=== 'cooldown'`` check never
matched and the friendly "Skipped" toast fell through to a generic
"Run failed". Hermetic: route-mock the run endpoint's 429 body so the exact
shape the server produces (jobs.py:538-546) exercises the real jsonApi/toast
code path, and pin the non-cooldown failure path stays unchanged.
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

_FAKE_JOB = {
    "id": "demo",
    "name": "Demo",
    "target_kind": "py",
    "schedule_chip": "",
    "next_run": None,
    "running": False,
    "stuck": False,
    "args": "",
    "schedule": {"type": "none"},
    "params": [],
    "last_run": None,
    "stats": {
        "p50": None, "p95": None, "success_rate_30d": None,
        "completed_count": 0, "last7": [],
    },
}


def _wire_jobs_list(page: Page) -> None:
    page.route(
        re.compile(r".*/api/jobs(\?.*)?$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"jobs": [_FAKE_JOB]}),
        ),
    )


def test_cooldown_429_shows_skipped_toast(authed_page: Page, base_url: str) -> None:
    _wire_jobs_list(authed_page)
    # FastAPI double-wraps our raise HTTPException(detail={...}) — this is
    # the exact body shape the server produces (jobs.py:538-546).
    cooldown_body = {
        "detail": {
            "detail": "cooldown",
            "retry_after_seconds": 42,
            "cooldown_seconds": 60,
        }
    }
    authed_page.route(
        re.compile(r".*/api/jobs/demo/run$"),
        lambda route: route.fulfill(
            status=429, content_type="application/json",
            body=_json.dumps(cooldown_body),
        ),
    )

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.wait_for_selector("#sessionsList", state="attached", timeout=5_000)
    authed_page.locator("#tabJobs").click()

    row = authed_page.locator("#jobsList li.app-item[data-id='demo']")
    expect(row).to_be_visible()
    row.locator("[data-role='run-btn']").click()

    expect(authed_page.locator(".toast")).to_contain_text("cooled down")
    expect(authed_page.locator(".toast")).not_to_contain_text("Run failed")


def test_non_cooldown_failure_still_shows_run_failed(
    authed_page: Page, base_url: str
) -> None:
    _wire_jobs_list(authed_page)
    authed_page.route(
        re.compile(r".*/api/jobs/demo/run$"),
        lambda route: route.fulfill(
            status=500, content_type="application/json",
            body=_json.dumps({"detail": "boom"}),
        ),
    )

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.wait_for_selector("#sessionsList", state="attached", timeout=5_000)
    authed_page.locator("#tabJobs").click()

    row = authed_page.locator("#jobsList li.app-item[data-id='demo']")
    expect(row).to_be_visible()
    row.locator("[data-role='run-btn']").click()

    expect(authed_page.locator(".toast")).to_contain_text("Run failed")
