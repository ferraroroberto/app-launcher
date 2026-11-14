"""Board: dismiss a failed job alert or a red CI card once it has been dealt with.

A failed or unconfirmed run's card, and a red CI workflow's, carries a dismiss
verb; tapping it POSTs the run's dismissal and drops the card, the lane count
and the header's "failing" count. A stuck or unreadable job card and an
unreadable CI repo's card describe a live problem and have no such verb.
Hermetic: the board and the dismiss routes are mocked, and the server-side
half is covered by tests/unit/jobs/test_jobs_dismissed.py and
tests/unit/board/test_ci_dismissed.py.
"""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.board._support import (
    _board_payload,
    _mock_board,
    _open_board,
    _route_board_from,
)
from tests.e2e.fixtures.waits import flush_requests, wait_until

pytestmark = pytest.mark.smoke


def _job(job_id: str, state: str, run_id: str | None) -> dict:
    return {
        "kind": "job", "job_id": job_id, "job_name": job_id.replace("-", " "),
        "state": state, "run_id": run_id,
        "finished_at": "2026-07-02T09:02:00" if state != "stuck" else None,
        "age_seconds": 600,
    }


def _ci(repo: str, workflow: str, run_id: int) -> dict:
    return {
        "kind": "ci", "repo": repo, "state": "failed", "workflow": workflow,
        "run_id": run_id, "run_url": f"https://github.com/example/{repo}/actions/runs/{run_id}",
        "conclusion": "failure", "age_seconds": 3600, "error": None,
    }


def test_dismissing_a_failed_job_or_ci_card_drops_it_and_the_failing_count(
    authed_page: Page, base_url: str
) -> None:
    payload = _board_payload()
    payload["columns"]["other"] = [
        _job("weekly-photos", "failed", "20260702T090200"),
        _job("digest", "unconfirmed", "20260702T091000"),
        _job("wedged", "stuck", "20260702T080000"),
        {"kind": "job", "job_id": "locked", "job_name": "locked",
         "state": "unreadable", "run_id": None, "finished_at": None,
         "age_seconds": None, "error": "[WinError 5] Access is denied"},
        _ci("alpha", "ci", 10),
        {"kind": "ci", "repo": "gamma", "state": "unknown", "workflow": None,
         "run_url": None, "age_seconds": None, "error": "HTTP 404"},
    ]
    _mock_board(authed_page)
    current = {"body": payload}
    _route_board_from(authed_page, current)

    dismissals: list[str] = []

    def _dismiss(route) -> None:
        url = route.request.url
        dismissals.append(f"{route.request.method} {url.split('/api/jobs/')[1]}")
        # What the server does: the dismissed run raises no card any more.
        current["body"] = {
            **current["body"],
            "columns": {
                **current["body"]["columns"],
                "other": [
                    c for c in current["body"]["columns"]["other"]
                    if c.get("run_id") != "20260702T090200"
                ],
            },
        }
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"dismissed": True}),
        )

    def _dismiss_ci(route) -> None:
        sent = _json.loads(route.request.post_data)
        dismissals.append(f"{route.request.method} ci {sent['repo']}/{sent['workflow']}/{sent['run_id']}")
        other = current["body"]["columns"]["other"]
        current["body"] = {
            **current["body"],
            "columns": {
                **current["body"]["columns"],
                "other": [c for c in other if c.get("run_id") != sent["run_id"]],
            },
        }
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"dismissed": True, **sent}),
        )

    authed_page.route(re.compile(r".*/api/jobs/[^/]+/runs/[^/]+/dismiss$"), _dismiss)
    authed_page.route(re.compile(r".*/api/board/ci/dismiss$"), _dismiss_ci)
    _open_board(authed_page, base_url)

    lane = authed_page.locator('.board-list[data-col="other"]')
    failed = lane.locator("li.board-item-job.is-failed")
    unconfirmed = lane.locator("li.board-item.is-unconfirmed")
    expect(failed).to_have_count(1)
    expect(failed.locator(".board-job-dismiss")).to_have_count(1)
    expect(unconfirmed.locator(".board-job-dismiss")).to_have_count(1)
    # Stuck and unreadable describe a live problem: no verb.
    expect(lane.locator("li.board-item.is-stuck .board-job-dismiss")).to_have_count(0)
    expect(lane.locator("li.board-item.is-unreadable .board-job-dismiss")).to_have_count(0)
    ci_failed = lane.locator("li.board-item-ci.is-failed")
    expect(ci_failed.locator(".board-ci-dismiss")).to_have_count(1)
    # An unreadable CI repo describes a live problem too: no verb.
    expect(lane.locator("li.board-item-ci.is-unknown .board-ci-dismiss")).to_have_count(0)
    expect(authed_page.locator("#boardColOther .board-count")).to_have_text("6")
    expect(authed_page.locator("#boardHeadStatus")).to_contain_text("3 failing")

    failed.locator(".board-job-dismiss").click()

    wait_until(authed_page, lambda: len(dismissals) == 1, "the dismiss POST")
    flush_requests(authed_page)
    assert dismissals == ["POST weekly-photos/runs/20260702T090200/dismiss"]
    expect(failed).to_have_count(0)
    expect(unconfirmed).to_have_count(1)
    expect(authed_page.locator("#boardColOther .board-count")).to_have_text("5")
    expect(authed_page.locator("#boardHeadStatus")).to_contain_text("2 failing")

    ci_failed.locator(".board-ci-dismiss").click()

    wait_until(authed_page, lambda: len(dismissals) == 2, "the CI dismiss POST")
    flush_requests(authed_page)
    assert dismissals[1] == "POST ci alpha/ci/10"
    expect(ci_failed).to_have_count(0)
    expect(lane.locator("li.board-item-ci.is-unknown")).to_have_count(1)
    expect(authed_page.locator("#boardColOther .board-count")).to_have_text("4")
    expect(authed_page.locator("#boardHeadStatus")).to_contain_text("1 failing")
