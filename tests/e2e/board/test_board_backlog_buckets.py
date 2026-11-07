"""Board e2e: the Backlog grouped into in progress, waiting on you, queued and not planned.

The payload is the server classifier's own output (src/board/backlog_buckets.py),
so the lane is proven against what the server sends, not a hand-written copy.
"""

from __future__ import annotations

import copy

import pytest
from playwright.sync_api import Page, expect

from src.board import backlog_buckets
from tests.e2e.board._support import _board_payload, _mock_board, _open_board, _unfold

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

LANE = "#boardColBacklog"


def _issue(number, title, labels=()):
    return {"kind": "issue", "repo": "app-launcher", "number": number, "title": title,
            "url": f"https://github.com/octo/app-launcher/issues/{number}",
            "updated_at": "2026-07-01T10:00:00Z", "labels": list(labels)}


def _payload(plan_state="ok"):
    issues = [
        _issue(11, "Lane holds this one"),
        _issue(12, "Shipped, needs a check", ["needs-validation"]),
        _issue(13, "Next after the docs fix"),
        _issue(14, "Nobody planned this"),
        _issue(15, "Nor this one"),
        _issue(16, "Umbrella kept open"),
    ]
    records = [_issue(90, "codebase-audit ledger", ["audit-meta"])]
    row = {"repo": "app-launcher", "status": "queued", "kind": "", "blocked_by": "", "after": ""}
    plan = {"state": plan_state, "waiting": [],
            "lanes": [{"repo": "app-launcher", "item": "#11", "status": "gate"}],
            "queue": [{**row, "ref": "#13", "after": "fleet-config#9"},
                      {**row, "ref": "#16", "status": "parked", "kind": "umbrella"}]}
    payload = _board_payload()
    health = backlog_buckets.classify(issues, records, plan, markers_read=True)
    payload["columns"]["backlog"] = copy.deepcopy(issues + records)
    payload["backlog_buckets"] = health
    return payload


def _headings(page: Page):
    return page.locator(f"{LANE} .board-bucket-heading")


def test_backlog_groups_issues_by_bucket_with_reasons(authed_page: Page, base_url: str) -> None:
    _mock_board(authed_page, _payload())
    _open_board(authed_page, base_url)
    # Folded, the lane says how much is in no plan instead of its count.
    expect(authed_page.locator(f"{LANE} .board-lane-exception")).to_have_text("2 not planned")
    _unfold(authed_page, "boardColBacklog")

    expect(_headings(authed_page)).to_have_text([
        "In progress (1)", "Waiting on you (1)", "Queued (1)", "Not planned (2)"])
    # Umbrellas and records are listed, folded shut, and never counted.
    expect(authed_page.locator(f"{LANE} .board-count")).to_have_text("5")
    fold = authed_page.locator(f"{LANE} .board-bucket-fold")
    expect(fold.locator("summary")).to_contain_text("Umbrellas and records (2)")
    expect(fold).not_to_have_attribute("open", "")

    def chips(bucket):
        return authed_page.locator(f"{LANE} .board-bucket[data-bucket='{bucket}'] .board-bucket-chip")
    expect(chips("in_progress")).to_have_text(["gate"])
    expect(chips("waiting")).to_have_text(["validate"])
    expect(chips("queued")).to_have_text(["after fleet-config#9"])


def test_unreadable_plan_shows_unknown_never_not_planned(authed_page: Page, base_url: str) -> None:
    _mock_board(authed_page, _payload(plan_state="unreadable"))
    _open_board(authed_page, base_url)
    _unfold(authed_page, "boardColBacklog")
    expect(_headings(authed_page)).to_have_text([
        "Unknown (6) · plan unreadable", "In progress (0)", "Waiting on you (0)",
        "Queued (0)", "Not planned (0)"])
    expect(authed_page.locator(f"{LANE} .board-lane-exception")).to_be_hidden()
