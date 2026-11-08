"""Board e2e: the Backlog's one per-issue partition, and the plan card reading it.

The payload is the server classifier's own output (src/board/backlog_buckets.py),
so the lane is proven against what the server sends, not a hand-written copy.
"""

from __future__ import annotations

import copy
import json

import pytest
from playwright.sync_api import Page, expect

from src.board import backlog_buckets
from src.chief import plan as chief_plan
from tests.e2e.board._support import _board_payload, _mock_board, _open_board, _unfold

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

LANE = "#boardColBacklog"


def _issue(number, title, labels=(), repo="app-launcher"):
    return {"kind": "issue", "repo": repo, "number": number, "title": title,
            "url": f"https://github.com/octo/{repo}/issues/{number}",
            "updated_at": "2026-07-01T10:00:00Z", "labels": list(labels)}


_PLAN_FILE = {
    "version": 1, "updated_at": "2026-07-01T10:00:00Z",
    "lanes": [{"repo": "app-launcher", "item": "#11", "status": "gate"}],
    "queue": [
        {"repo": "app-launcher", "ref": "#13", "title": "Next after the docs fix",
         "status": "queued", "after": "fleet-config#9"},
        {"repo": "app-launcher", "ref": "#16", "title": "Umbrella kept open",
         "status": "parked", "kind": "umbrella"},
        {"repo": "app-launcher", "ref": "#17", "title": "Waits on the docs fix",
         "status": "parked", "kind": "blocked-by", "blocked_by": "app-launcher#13"},
        {"repo": "app-launcher", "ref": "#18", "title": "Owner row without the label",
         "status": "parked", "kind": "owner"},
    ],
}


def _payload(tmp_path, plan_state="ok"):
    issues = [
        _issue(11, "Lane holds this one"),
        _issue(12, "Shipped, needs a check", ["needs-validation"]),
        _issue(13, "Next after the docs fix"),
        _issue(14, "Nobody planned this"),
        _issue(15, "Nor this one"),
        _issue(16, "Umbrella kept open"),
        _issue(17, "Waits on the docs fix"),
        _issue(18, "Owner row without the label"),
        _issue(3, "Old idea in a resting repo", repo="sleepy"),
    ]
    records = [_issue(90, "codebase-audit ledger", ["audit-meta"])]
    path = tmp_path / "chief-plan.json"
    path.write_text(json.dumps(_PLAN_FILE), encoding="utf-8")
    index = chief_plan.read_plan_index(path)
    if plan_state != "ok":
        index = {"state": plan_state, "lanes": [], "queue": [], "waiting": []}
    payload = _board_payload()
    health = backlog_buckets.classify(
        issues, records, index, markers_read=True, dormant_repos={"sleepy"})
    payload["columns"]["backlog"] = copy.deepcopy(issues + records)
    payload["backlog_buckets"] = health
    return payload, chief_plan.read_chief_plan(path, "octo")


def _headings(page: Page):
    return page.locator(f"{LANE} .board-bucket-heading")


def test_backlog_counts_every_issue_in_one_bucket(authed_page: Page, base_url: str, tmp_path) -> None:
    payload, _ = _payload(tmp_path)
    _mock_board(authed_page, payload)
    _open_board(authed_page, base_url)
    # Folded, the lane says how much is in no plan instead of its count.
    expect(authed_page.locator(f"{LANE} .board-lane-exception")).to_have_text("2 not planned")
    _unfold(authed_page, "boardColBacklog")

    expect(_headings(authed_page)).to_have_text([
        "In progress (1)", "Waiting on you (1)", "Queued (1)", "Blocked (2)", "Not planned (2)"])
    # Every open issue counts, the folded buckets included, and the sum line
    # adds them up to the lane's count.
    expect(authed_page.locator(f"{LANE} .board-count")).to_have_text("10")
    expect(authed_page.locator(f"{LANE} .board-bucket-sum-line")).to_have_text(
        "10 open issues: 1 in progress · 1 waiting on you · 1 queued · 2 blocked"
        " · 2 not planned · 1 umbrellas · 1 records · 1 dormant")
    folds = authed_page.locator(f"{LANE} .board-bucket-fold summary")
    expect(folds).to_have_text(["Umbrellas (1)", "Records (1)", "Dormant (1)"])
    expect(authed_page.locator(f"{LANE} .board-bucket-fold").first).not_to_have_attribute("open", "")

    def chips(bucket):
        return authed_page.locator(f"{LANE} .board-bucket[data-bucket='{bucket}'] .board-bucket-chip")
    expect(chips("in_progress")).to_have_text(["gate"])
    expect(chips("waiting")).to_have_text(["validate"])
    expect(chips("queued")).to_have_text(["after fleet-config#9"])
    expect(chips("blocked")).to_have_text(["blocked by app-launcher#13", "owner step · no label"])


def test_plan_groups_name_and_count_the_backlog_buckets(authed_page: Page, base_url: str, tmp_path) -> None:
    payload, plan = _payload(tmp_path)
    _mock_board(authed_page, payload, plan=plan)
    _open_board(authed_page, base_url)
    plan_body = authed_page.locator("#boardChiefPlan .board-plan-body")
    expect(plan_body).to_have_attribute("data-state", "ok")
    # Same words and counts as the Backlog's Queued, Blocked and Umbrellas.
    expect(plan_body.locator(".board-plan-heading")).to_have_text(
        ["Lanes (1)", "Queued (1)", "Blocked (2)", "Umbrellas (1)"])


def test_unreadable_plan_shows_unknown_never_not_planned(authed_page: Page, base_url: str, tmp_path) -> None:
    payload, _ = _payload(tmp_path, plan_state="unreadable")
    _mock_board(authed_page, payload)
    _open_board(authed_page, base_url)
    _unfold(authed_page, "boardColBacklog")
    expect(_headings(authed_page)).to_have_text([
        "Unknown (9) · plan unreadable", "In progress (0)", "Waiting on you (0)",
        "Queued (0)", "Blocked (0)", "Not planned (0)"])
    expect(authed_page.locator(f"{LANE} .board-lane-exception")).to_be_hidden()
