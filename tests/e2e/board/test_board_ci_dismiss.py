"""Board: dismiss a red CI card once it has been dealt with.

A failed workflow's card carries the same check button a failed Job's does;
tapping it POSTs the run's dismissal and drops the card, the lane count and
the header's "failing" count. An unreadable repo's card describes a live
problem and has no such verb. Hermetic: the board and the dismiss route are
mocked, and the server-side half (the store, ``board_cards``, the route) is
covered by tests/unit/board/test_ci_dismissed.py.
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


def _ci(repo: str, workflow: str, run_id: int) -> dict:
    return {
        "kind": "ci", "repo": repo, "state": "failed", "workflow": workflow,
        "run_id": run_id, "run_url": f"https://github.com/example/{repo}/actions/runs/{run_id}",
        "conclusion": "failure", "age_seconds": 3600, "error": None,
    }


def test_dismissing_a_red_ci_card_drops_it_and_the_failing_count(
    authed_page: Page, base_url: str
) -> None:
    payload = _board_payload()
    payload["columns"]["other"] = [
        _ci("alpha", "ci", 10),
        _ci("beta", "lint", 11),
        {"kind": "ci", "repo": "gamma", "state": "unknown", "workflow": None,
         "run_url": None, "age_seconds": None, "error": "HTTP 404"},
    ]
    _mock_board(authed_page)
    current = {"body": payload}
    _route_board_from(authed_page, current)

    dismissals: list[dict] = []

    def _dismiss(route) -> None:
        sent = _json.loads(route.request.post_data)
        dismissals.append({"method": route.request.method, **sent})
        # What the server does: the dismissed run raises no card any more.
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

    authed_page.route(re.compile(r".*/api/board/ci/dismiss$"), _dismiss)
    _open_board(authed_page, base_url)

    lane = authed_page.locator('.board-list[data-col="other"]')
    failed = lane.locator("li.board-item-ci.is-failed")
    expect(failed).to_have_count(2)
    expect(failed.locator(".board-ci-dismiss")).to_have_count(2)
    # An unreadable repo describes a live problem: no verb.
    expect(lane.locator("li.board-item-ci.is-unknown .board-ci-dismiss")).to_have_count(0)
    expect(authed_page.locator("#boardColOther .board-count")).to_have_text("3")
    expect(authed_page.locator("#boardHeadStatus")).to_contain_text("2 failing")

    failed.first.locator(".board-ci-dismiss").click()

    wait_until(authed_page, lambda: len(dismissals) == 1, "the dismiss POST")
    flush_requests(authed_page)
    assert dismissals == [{"method": "POST", "repo": "alpha", "workflow": "ci", "run_id": 10}]
    expect(failed).to_have_count(1)
    expect(authed_page.locator("#boardColOther .board-count")).to_have_text("2")
    expect(authed_page.locator("#boardHeadStatus")).to_contain_text("1 failing")
