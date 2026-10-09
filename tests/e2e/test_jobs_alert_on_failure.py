"""Per-job Telegram alert-on-failure toggle + its indicator (issue #597).

Hermetic route mocks: the dialog toggle is a vendored `switch` control
(same contract as `#jobConfirmInput`, covered generally by
test_vendored_switch.py); these tests pin the two feature-specific
bits — the toggle sits before "Require confirmation", and the indicator
is gated on `job.alert_on_failure`. #1438 moved that indicator: the bell
that sat on the job row is now the "Alerts" line of the job sheet's detail
block ("Telegram, on failure"), and the row's Edit lives in its kebab (there
is no Edit mode to enter first).
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.smoke

_BASE_JOB = {
    "id": "demo",
    "name": "Demo",
    "script_path": "C:\\stub\\demo.py",
    "target_kind": "py",
    "kind": "python",
    "kind_config": {},
    "args": "",
    "schedule": {"type": "none"},
    "schedule_chip": "",
    "next_run": None,
    "running": False,
    "stuck": False,
    "paused": False,
    "confirm": False,
    "on_success": [],
    "on_failure": [],
    "params": [],
    "last_run": None,
    "stats": {
        "p50": None, "p95": None, "success_rate_30d": None,
        "completed_count": 0, "last7": [],
    },
}


def _wire_jobs_list(page: Page, jobs: list) -> None:
    page.route(
        re.compile(r".*/api/jobs(\?.*)?$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"jobs": jobs}),
        ),
    )


def test_bell_icon_dialog_toggle_order_and_save(
    authed_page: Page, base_url: str
) -> None:
    """The sheet's Alerts line, the dialog toggle's placement and its Save
    round-trip on one page load (#1215). ``demo`` has no ``alert_on_failure``
    flag, so it is also the "off" job of the Alerts-line check (the old
    ``quiet`` job was the same ``_BASE_JOB`` under another id); the Save — the
    only step that writes — runs last."""
    on_job = dict(_BASE_JOB, id="alerted", name="Alerted", alert_on_failure=True)
    _wire_jobs_list(authed_page, [dict(_BASE_JOB), on_job])
    captured = {}

    def _handle_put(route):
        captured["body"] = _json.loads(route.request.post_data or "{}")
        payload = dict(_BASE_JOB)
        payload["alert_on_failure"] = True
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"job": payload}),
        )

    authed_page.route(re.compile(r".*/api/jobs/demo$"), _handle_put)
    authed_page.route(
        re.compile(r".*/api/jobs/[^/]+/runs$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps({"runs": []})),
    )

    authed_page.goto(base_url, wait_until="domcontentloaded")
    authed_page.locator("#tabJobs").click()

    # -- was test_bell_icon_shown_only_when_flag_set (the bell left the row in
    # #1438; the fact is the job sheet's Alerts line) --
    on_row = authed_page.locator("#jobsList li[data-id='alerted']")
    off_row = authed_page.locator("#jobsList li[data-id='demo']")
    expect(authed_page.locator("[data-role='alert-icon']")).to_have_count(0)
    on_row.locator(".action-row-main").click()
    alerts = authed_page.locator("#jobSheetBody [data-role='alerts-line']")
    expect(alerts).to_have_count(1)
    expect(alerts).to_contain_text("Telegram, on failure")
    authed_page.locator("#jobSheetClose").click()
    off_row.locator(".action-row-main").click()
    expect(authed_page.locator("#jobSheetTitle")).to_have_text("Demo")
    expect(authed_page.locator("#jobSheetBody [data-role='alerts-line']")).to_have_count(0)
    authed_page.locator("#jobSheetClose").click()
    expect(authed_page.locator("#jobSheet")).to_be_hidden()

    # -- was test_toggle_precedes_confirm_toggle_in_dialog --
    # Edit is in the row's ⋯ menu (#1130), reachable without an Edit mode (#1438).
    off_row.locator(".action-row-kebab").click()
    off_row.locator(".job-edit-item").click()

    expect(authed_page.locator("#jobDialog")).to_be_visible()
    alert_toggle = authed_page.locator("#jobAlertOnFailureInput")
    confirm_toggle = authed_page.locator("#jobConfirmInput")
    expect(alert_toggle).to_have_attribute("role", "switch")
    expect(alert_toggle).to_have_attribute("aria-checked", "false")

    # DOM order: the alert toggle's row precedes the confirm toggle's row.
    rows = authed_page.locator(".job-alert-row, .job-confirm-row")
    expect(rows).to_have_count(2)
    expect(rows.nth(0)).to_have_class(re.compile("job-alert-row"))
    expect(rows.nth(1)).to_have_class(re.compile("job-confirm-row"))

    # -- was test_saving_toggle_sends_alert_on_failure (last: writes) --
    authed_page.locator("#jobAlertOnFailureInput").click()
    authed_page.locator("#jobSaveBtn").click()

    expect(authed_page.locator("#jobDialog")).to_be_hidden()
    assert captured["body"].get("alert_on_failure") is True
