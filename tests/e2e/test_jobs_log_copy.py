"""Regression pin for issue #97 (Jobs tab: tap a run's log to copy it).

Tapping the expanded run-output pane (``[data-role="output-tail"]``) must
copy the full log text to the clipboard and confirm with a toast. The
handler lives in ``jobs.js`` (``copyOutputTail`` wired in
``renderHistoryLi``).

Hermetic: route-mock ``/api/jobs`` + the run-list and run-detail endpoints
so the expand → select → output flow runs through the production code path,
and mock ``navigator.clipboard.writeText`` via init script (headless WebKit
clipboard perms are not reliable, same reason as the #29 paste test). The
check runs in both projections.
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

_LOG_TEXT = "Traceback (most recent call last):\n  ValueError: boom-{97}\n"

# Capture writeText payloads on window.__copied. defineProperty mirrors the
# #29 paste mock — navigator.clipboard is non-writable in some WebKit
# contexts, so direct assignment fails.
_CLIPBOARD_MOCK = """
(() => {
  window.__copied = [];
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: {
      writeText: async (t) => { window.__copied.push(t); },
      readText: async () => '',
    },
  });
})()
"""


def _wire_job_routes(page: Page) -> None:
    fake_job = {
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
        "last_run": {
            "run_id": "20260529T120000", "status": "failed",
            "started_at": "2026-05-29T12:00:00", "duration_seconds": 1.0,
        },
        "stats": {
            "p50": None, "p95": None, "success_rate_30d": None,
            "completed_count": 1, "last7": [],
        },
    }
    run_summary = {
        "run_id": "20260529T120000", "status": "failed",
        "started_at": "2026-05-29T12:00:00", "trigger": "manual",
        "exit_code": 1, "dry_run": False, "params": {},
    }
    run_detail = {
        "run": {
            "run_id": "20260529T120000", "status": "failed",
            "output_tail": _LOG_TEXT, "exit_code": 1,
            "cpu_seconds": None, "peak_rss_bytes": None,
            "duration_seconds": 1.0,
        }
    }

    # Most specific first; the three regexes are mutually exclusive anyway.
    page.route(
        re.compile(r".*/api/jobs/demo/runs/[^/]+$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(run_detail),
        ),
    )
    page.route(
        re.compile(r".*/api/jobs/demo/runs$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"runs": [run_summary]}),
        ),
    )
    page.route(
        re.compile(r".*/api/jobs(\?.*)?$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"jobs": [fake_job]}),
        ),
    )


def test_tapping_job_log_copies_to_clipboard(
    authed_page: Page, base_url: str
) -> None:
    authed_page.add_init_script(_CLIPBOARD_MOCK)
    _wire_job_routes(authed_page)

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.wait_for_selector("#sessionsList", state="attached", timeout=5_000)
    authed_page.locator("#tabJobs").click()

    # Expand the job row → the output pane mounts and fills with the run's log.
    row = authed_page.locator("#jobsList li.app-item[data-id='demo']")
    expect(row).to_be_visible()
    row.locator("button.session-open").click()

    tail = authed_page.locator("[data-role='output-tail']")
    expect(tail).to_contain_text("boom-{97}")

    # Tap the log → writeText fires with the full text, toast confirms.
    tail.click()
    authed_page.wait_for_function(
        "() => Array.isArray(window.__copied) && window.__copied.length > 0",
        timeout=3_000,
    )
    copied = authed_page.evaluate("() => window.__copied[0]")
    assert copied == _LOG_TEXT, (
        f"tap-to-copy delivered {copied!r}, expected the full run log"
    )
    expect(authed_page.locator(".toast")).to_contain_text("Copied log")
