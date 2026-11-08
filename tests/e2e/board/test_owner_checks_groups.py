"""Your turn's owner-check groups reconcile with the Backlog's Waiting on you.

The payload is the server's own output (``classify`` then ``owner_counts``), so
the browser is proven against what the server sends: three disjoint groups, a
badge that is their counts added, Waiting on you as the two open groups, and a
Validate to close row that names the plan rows it unblocks.
"""

from __future__ import annotations

import copy

import pytest
from playwright.sync_api import Page, expect

from src.board import backlog_buckets
from tests.e2e.board._support import _board_payload, _mock_board, _open_board, _unfold

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_REPO = "app-launcher"


def _issue(number, title, labels=()):
    return {"kind": "issue", "repo": _REPO, "number": number, "title": title,
            "url": f"https://github.com/octo/{_REPO}/issues/{number}",
            "updated_at": "2026-07-01T10:00:00Z", "labels": list(labels)}


def _check(number, title, kind, state="OPEN", mark=None):
    return {"ref": f"{_REPO}#{number}", "repo": _REPO, "number": number, "title": title,
            "url": f"https://github.com/octo/{_REPO}/issues/{number}", "state": state,
            "kind": kind, "block": True, "reason": "a real device", "try": "tap it",
            "proof": [], "since": "2026-10-30", "mark": mark, "mark_at": None, "mark_until": None}


_PLAN = {"state": "ok", "lanes": [], "waiting": [], "queue": [
    {"repo": _REPO, "ref": "#5", "status": "parked", "kind": "blocked-by",
     "blocked_by": f"{_REPO}#3", "after": ""},
]}


def _payload() -> dict:
    """Two steps before start, two to validate (one holds up a plan row), and
    two shipped checks to try, one of them a reported problem."""
    issues = [
        _issue(1, "Pair the speaker", ["needs-owner"]),
        _issue(2, "Sign the form", ["needs-owner"]),
        _issue(3, "Check the lock screen", ["needs-validation"]),
        _issue(4, "Check the swipe", ["needs-validation"]),
        _issue(5, "Waits on the lock screen"),
    ]
    items = [
        _check(1, "Pair the speaker", "before-start"),
        _check(2, "Sign the form", "before-start"),
        _check(3, "Check the lock screen", "validate-after"),
        _check(4, "Check the swipe", "validate-after"),
        _check(20, "Shipped read-aloud", "validate-after", "CLOSED"),
        _check(21, "Shipped archive", "validate-after", "CLOSED", mark="problem"),
    ]
    payload = _board_payload()
    payload["columns"]["your_turn"] = []
    health = backlog_buckets.classify(
        issues, [], _PLAN, markers_read=True, dormant_repos=set())
    health["your_turn"] = backlog_buckets.owner_counts(issues, items, _PLAN)
    payload["columns"]["backlog"] = copy.deepcopy(issues)
    payload["backlog_buckets"] = health
    payload["owner_checks"] = {
        "available": True, "fetched_at": payload["github"]["fetched_at"], "error": None,
        "refreshing": False, "unknown": [], "items": items,
    }
    return payload


def test_your_turn_groups_add_up_to_the_badge_and_to_waiting_on_you(
    authed_page: Page, base_url: str
) -> None:
    _mock_board(authed_page, _payload())
    _open_board(authed_page, base_url)

    lane = authed_page.locator("#boardColYours")
    headings = lane.locator(".board-owner-group > summary")
    # The reported problem keeps its row in Try what shipped and is said, not counted.
    expect(headings).to_have_text([
        "Before you start (2)", "Validate to close (2)", "Try what shipped (1 · 1 not counted)"])
    # The badge is the three counts added, and the lane says the same.
    expect(authed_page.locator("#tabBoard .tab-badge")).to_have_text("5")
    expect(lane.locator(".board-count")).to_have_text("5")
    # The reconciliation is said in words.
    expect(lane.locator(".board-owner-state")).to_contain_text(
        "4 waiting on you (2 before you start + 2 validate to close) · 1 to try")

    # No issue is in two groups.
    rows = lane.locator(".board-owner-group li.board-owner-row")
    expect(rows).to_have_count(6)
    refs = [row.get_attribute("data-ref") for row in rows.all()]
    assert len(refs) == len(set(refs)), refs

    # Waiting on you on the Backlog is Before you start plus Validate to close.
    _unfold(authed_page, "boardColBacklog")
    expect(authed_page.locator(
        "#boardColBacklog .board-bucket[data-bucket='waiting'] > .board-bucket-fold > summary"
    )).to_have_text("Waiting on you (4)")


def test_a_validate_to_close_row_names_the_plan_rows_it_unblocks(
    authed_page: Page, base_url: str
) -> None:
    _mock_board(authed_page, _payload())
    _open_board(authed_page, base_url)

    holds = authed_page.locator(f'#boardColYours .board-owner-row[data-ref="{_REPO}#3"]')
    expect(holds).to_contain_text(f"unblocks {_REPO}#5")
    holds.locator(".action-row-main").click()
    expect(holds.locator(".board-owner-detail")).to_contain_text(f"Unblocks: {_REPO}#5")
    free = authed_page.locator(f'#boardColYours .board-owner-row[data-ref="{_REPO}#4"]')
    expect(free).not_to_contain_text("unblocks")
