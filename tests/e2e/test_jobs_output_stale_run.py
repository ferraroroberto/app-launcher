"""Regression pin for app-launcher#1630 (Jobs sheet: a late reply painted the wrong run).

``selectRun`` fires ``refreshOutputForRun`` for the tapped run without
cancelling the one before it, and ``writeOutput`` painted whichever reply
arrived. Open a job's sheet (newest run A is selected and fetched), tap the
older run B, and if A's slower ``/runs/A`` reply lands *after* B's, B stays
highlighted while the output pane and its "Output · A" label show A. The
bigger a run's output tail, the slower its reply, so tapping from a long run
to a short one is exactly that case.

Deterministic, not timing-based: the run-detail route *holds* every reply,
the test fulfils B's first and A's last, so the out-of-order arrival is the
test's doing rather than a guess about the network. Against the pre-fix code
the pane ends on A's log; with the ``writeOutput`` guard it stays on B's.

Both runs are final (``failed``), so no live stream and no re-poll of the
output pane interferes.
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import flush_requests, wait_until

pytestmark = pytest.mark.smoke

_RUN_A = "20260529T120000"  # newest: the sheet's default selection
_RUN_B = "20260529T110000"  # older: the one the user taps
_LOGS = {_RUN_A: "log-of-run-A-1630\n", _RUN_B: "log-of-run-B-1630\n"}


def _run_summary(run_id: str) -> dict:
    return {
        "run_id": run_id, "status": "failed",
        "started_at": "2026-05-29T12:00:00", "trigger": "manual",
        "exit_code": 1, "dry_run": False, "params": {},
    }


def _run_detail(run_id: str) -> dict:
    return {"run": {
        "run_id": run_id, "status": "failed", "output_tail": _LOGS[run_id],
        "exit_code": 1, "cpu_seconds": None, "peak_rss_bytes": None,
        "duration_seconds": 1.0,
    }}


def _wire(page: Page, held: list) -> None:
    fake_job = {
        "id": "demo", "name": "Demo", "target_kind": "py", "schedule_chip": "",
        "next_run": None, "running": False, "stuck": False, "args": "",
        "schedule": {"type": "none"}, "params": [],
        "last_run": _run_summary(_RUN_A),
        "stats": {
            "p50": None, "p95": None, "success_rate_30d": None,
            "completed_count": 2, "last7": [],
        },
    }

    def _json_route(body: dict):
        return lambda route: route.fulfill(
            status=200, content_type="application/json", body=_json.dumps(body)
        )

    def _hold_detail(route) -> None:
        run_id = route.request.url.rsplit("/", 1)[-1].split("?")[0]
        held.append((run_id, route))

    page.route(re.compile(r".*/api/jobs/demo/runs/[^/?]+(\?.*)?$"), _hold_detail)
    page.route(
        re.compile(r".*/api/jobs/demo/runs(\?.*)?$"),
        _json_route({"runs": [_run_summary(_RUN_A), _run_summary(_RUN_B)]}),
    )
    page.route(re.compile(r".*/api/jobs(\?.*)?$"), _json_route({"jobs": [fake_job]}))
    # A boot fetch that never answers must not hold the Jobs tab back (#1258).
    page.route(re.compile(r".*/api/claude-code/git-status$"), lambda route: None)


def _held_for(held: list, run_id: str) -> list:
    return [route for rid, route in held if rid == run_id]


def _release(held: list, run_id: str) -> None:
    for route in _held_for(held, run_id):
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(_run_detail(run_id)),
        )
    held[:] = [(rid, route) for rid, route in held if rid != run_id]


def test_late_reply_for_a_deselected_run_does_not_repaint_the_pane(
    authed_page: Page, base_url: str
) -> None:
    held: list = []
    _wire(authed_page, held)

    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    authed_page.wait_for_selector("#sessionsList", state="attached", timeout=5_000)
    authed_page.locator("#tabJobs").click()

    row = authed_page.locator("#jobsList li.job-row[data-id='demo']")
    expect(row).to_be_visible()
    row.locator("button.action-row-main").click()

    # The sheet opened on the newest run A and asked for its detail (held).
    wait_until(authed_page, lambda: bool(_held_for(held, _RUN_A)),
               "the sheet's first fetch of run A")

    # Tap the older run B while A's reply is still in flight.
    run_buttons = authed_page.locator("#jobSheetBody [data-role='runs-list'] button.jobs-run-btn")
    expect(run_buttons).to_have_count(2)
    run_buttons.nth(1).click()
    wait_until(authed_page, lambda: bool(_held_for(held, _RUN_B)),
               "the fetch of run B after tapping it")

    # B answers first, then A's slower reply lands last.
    _release(held, _RUN_B)
    tail = authed_page.locator("#jobSheetBody [data-role='output-tail']")
    label = authed_page.locator("#jobSheetBody [data-role='output-label']")
    expect(tail).to_contain_text("log-of-run-B-1630")
    _release(held, _RUN_A)
    flush_requests(authed_page)

    expect(run_buttons.nth(1)).to_have_class(re.compile(r"\bselected\b"))
    expect(tail).to_contain_text("log-of-run-B-1630")
    expect(tail).not_to_contain_text("log-of-run-A-1630")
    expect(label).to_contain_text(_RUN_B)
    expect(label).not_to_contain_text(_RUN_A)
