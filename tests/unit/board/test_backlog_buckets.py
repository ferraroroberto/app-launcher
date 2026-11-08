"""Backlog buckets: one fixture per precedence rule, the unknown states, and
the reconciliation every Board count rests on."""

from __future__ import annotations

import json

import pytest

from src.board import backlog_buckets as bb
from src.chief.plan import read_plan_index


def _card(number, labels=(), in_progress=False, repo="app-launcher"):
    return {"kind": "issue", "repo": repo, "number": number,
            "labels": list(labels), "in_progress": in_progress}


def _plan(lanes=(), queue=(), waiting=()):
    return {"state": "ok", "lanes": list(lanes), "queue": list(queue), "waiting": list(waiting)}


def _row(n, status="queued", **extra):
    return {"repo": "app-launcher", "ref": f"#{n}", "status": status, "kind": "",
            "blocked_by": "", "after": "", **extra}


def _place(card, plan, markers_read=True, dormant=frozenset()):
    health = bb.classify([card], [], plan, markers_read=markers_read, dormant_repos=set(dormant))
    return card["bucket"], card["bucket_chips"], health


@pytest.mark.parametrize("card,plan,bucket,chips", [
    # 1. In progress: a marker, or a lane, beats every lower rule.
    (_card(1, ["needs-owner"], in_progress=True), _plan(), bb.IN_PROGRESS, []),
    (_card(1, ["needs-validation"]),
     _plan(lanes=[{"repo": "app-launcher", "item": "#1", "status": "gate"}], queue=[_row(1)]),
     bb.IN_PROGRESS, ["gate"]),
    # 2. Waiting on you: the label, and nothing else.
    (_card(2, ["needs-owner"]), _plan(queue=[_row(2)]), bb.WAITING, ["before start"]),
    (_card(2, ["Needs-Validation"]), _plan(), bb.WAITING, ["validate"]),
    (_card(2, ["needs-owner"]), _plan(waiting=[{"ref": "app-launcher#2", "repo": "app-launcher"}]),
     bb.WAITING, ["before start", "question"]),
    # A question or a waiting-roberto row without the label is a chip, not the bucket.
    (_card(2), _plan(waiting=[{"ref": "app-launcher#2", "repo": "app-launcher"}]),
     bb.NOT_PLANNED, ["question"]),
    (_card(2), _plan(queue=[_row(2, "waiting-roberto")]), bb.QUEUED, ["question"]),
    # An owner row that lacks the label is Blocked, and says why.
    (_card(2), _plan(queue=[_row(2, "parked", kind="owner")]), bb.BLOCKED, ["owner step", "no label"]),
    (_card(2, ["needs-owner"]), _plan(queue=[_row(2, "parked", kind="owner")]),
     bb.WAITING, ["before start"]),
    # 3. Queued, with its reasons; building with no lane or marker stays queued.
    (_card(3), _plan(queue=[_row(3)]), bb.QUEUED, []),
    (_card(3), _plan(queue=[_row(3, "building")]), bb.QUEUED, ["building"]),
    (_card(3), _plan(queue=[_row(3, after="fleet-config#9 no-lane:fleet-config")]),
     bb.QUEUED, ["after fleet-config#9", "after no lane in fleet-config"]),
    # Blocked: blocked-by rows, and a parked row with no kind.
    (_card(3), _plan(queue=[_row(3, "parked", kind="blocked-by", blocked_by="life-os#4")]),
     bb.BLOCKED, ["blocked by life-os#4"]),
    (_card(3), _plan(queue=[_row(3, "parked")]), bb.BLOCKED, ["parked"]),
    # Umbrellas.
    (_card(5), _plan(queue=[_row(5, "parked", kind="umbrella")]), bb.UMBRELLA, []),
    # 4. Not planned; a row for another repo's #4 does not match.
    (_card(4), _plan(queue=[{**_row(4), "repo": "life-os"}]), bb.NOT_PLANNED, []),
])
def test_precedence(card, plan, bucket, chips):
    got_bucket, got_chips, _ = _place(card, plan)
    assert (got_bucket, got_chips) == (bucket, chips)


def test_dormant_repo_takes_what_no_row_places():
    assert _place(_card(6, repo="sleepy"), _plan(), dormant={"sleepy"})[0] == bb.DORMANT
    # A row, or the label, still places it first.
    queued = _plan(queue=[{**_row(6), "repo": "sleepy"}])
    assert _place(_card(6, repo="sleepy"), queued, dormant={"sleepy"})[0] == bb.QUEUED
    assert _place(_card(6, ["needs-owner"], repo="sleepy"), _plan(), dormant={"sleepy"})[0] == bb.WAITING


def test_unreadable_scope_places_as_active_and_says_unknown():
    card = _card(6, repo="sleepy")
    health = bb.classify([card], [], _plan(), markers_read=True, dormant_repos=None)
    assert card["bucket"] == bb.NOT_PLANNED and health["scope"] == "unknown"


def test_unreadable_plan_is_unknown_never_not_planned():
    plan = {"state": "unreadable", "lanes": [], "queue": [], "waiting": []}
    card = _card(7, ["needs-owner"])
    bucket, _, health = _place(card, plan)
    assert bucket == bb.UNKNOWN
    assert (health["plan"], health["markers"], health["unknown_reason"]) == (
        "unreadable", "ok", "plan unreadable")
    held = _card(8, in_progress=True)
    assert _place(held, plan)[0] == bb.IN_PROGRESS
    record = _card(9, ["audit-meta"])
    bb.classify([], [record], plan, markers_read=True, dormant_repos=set())
    assert record["bucket"] == bb.RECORD


def test_unreadable_markers_are_unknown_but_a_lane_still_places():
    bucket, _, health = _place(_card(7), _plan(), markers_read=False)
    assert bucket == bb.UNKNOWN and health["unknown_reason"] == "issue markers unreadable"
    lane = _plan(lanes=[{"repo": "app-launcher", "item": "#7", "status": ""}])
    assert _place(_card(7), lane, markers_read=False)[:2] == (bb.IN_PROGRESS, ["lane"])


def _fixture_board():
    """One issue per bucket shape, a plan that names most of them, owner checks."""
    cards = [
        _card(1, in_progress=True),
        _card(2, ["needs-owner"]),
        _card(3, ["needs-validation"]),
        _card(4),  # queued
        _card(5),  # blocked-by
        _card(6),  # owner row without the label
        _card(7),  # umbrella
        _card(8),  # not planned
        _card(9, repo="sleepy"),  # dormant
        _card(10, ["needs-owner"]),  # waiting, snoozed by the owner
    ]
    records = [_card(90, ["audit-meta"]), _card(91, ["audit-meta"])]
    plan = _plan(queue=[
        _row(2, "parked", kind="owner"),
        _row(4),
        _row(5, "parked", kind="blocked-by", blocked_by="life-os#1"),
        _row(6, "parked", kind="owner"),
        _row(7, "parked", kind="umbrella"),
        _row(4, "building"),  # a repeat row: the first one places the issue
    ])
    owner_items = [
        {"ref": "app-launcher#2", "repo": "app-launcher", "number": 2, "state": "OPEN", "mark": None},
        {"ref": "app-launcher#3", "repo": "app-launcher", "number": 3, "state": "OPEN", "mark": None},
        {"ref": "app-launcher#10", "repo": "app-launcher", "number": 10, "state": "OPEN", "mark": "later"},
        {"ref": "app-launcher#50", "repo": "app-launcher", "number": 50, "state": "CLOSED", "mark": None},
        {"ref": "app-launcher#51", "repo": "app-launcher", "number": 51, "state": "CLOSED", "mark": None},
        # In progress yet still labelled: listed, not counted.
        {"ref": "app-launcher#1", "repo": "app-launcher", "number": 1, "state": "OPEN", "mark": None},
    ]
    return cards, records, plan, owner_items


def test_board_counts_reconcile_per_issue():
    cards, records, plan, owner_items = _fixture_board()
    section = bb.classify(cards, records, plan, markers_read=True, dormant_repos={"sleepy"})
    every = cards + records
    counts = section["counts"]

    # Each open issue sits in exactly one bucket, and the buckets sum to the total.
    assert all(isinstance(c["bucket"], str) for c in every)
    assert section["total"] == len(every) == sum(counts.values())
    for bucket, n in counts.items():
        assert n == sum(1 for c in every if c["bucket"] == bucket), bucket
    assert counts == {
        bb.UNKNOWN: 0, bb.IN_PROGRESS: 1, bb.WAITING: 3, bb.QUEUED: 1, bb.BLOCKED: 2,
        bb.NOT_PLANNED: 1, bb.UMBRELLA: 1, bb.RECORD: 2, bb.DORMANT: 1,
    }

    # The plan's groups, read by issue, equal the matching bucket counts.
    by_key = {(c["repo"], c["number"]): c["bucket"] for c in every}
    planned = {}
    for row in plan["queue"]:
        key = ("app-launcher", int(row["ref"].lstrip("#")))
        planned.setdefault(key, by_key[key])
    for bucket in (bb.QUEUED, bb.BLOCKED, bb.UMBRELLA):
        assert sum(1 for b in planned.values() if b == bucket) == counts[bucket], bucket
    assert sum(1 for b in planned.values() if b == bb.WAITING) <= counts[bb.WAITING]

    # Your turn is Waiting on you plus to-validate, less what the owner set aside.
    turn = bb.owner_counts(every, owner_items)
    assert turn == {"waiting": 3, "to_validate": 2, "set_aside": 1, "needs_you": 4, "unlisted": 0}
    assert turn["needs_you"] == turn["waiting"] + turn["to_validate"] - turn["set_aside"] - turn["unlisted"]
    assert turn["needs_you"] == sum(1 for it in owner_items if it["counted"])
    assert {it["ref"]: it["bucket"] for it in owner_items}["app-launcher#1"] == bb.IN_PROGRESS


def test_owner_counts_name_waiting_issues_the_owner_list_lacks():
    cards = [_card(2, ["needs-owner"]), _card(3, ["needs-owner"])]
    bb.classify(cards, [], _plan(), markers_read=True, dormant_repos=set())
    items = [{"ref": "app-launcher#2", "repo": "app-launcher", "number": 2, "state": "OPEN", "mark": None},
             {"ref": "app-launcher#77", "repo": "app-launcher", "number": 77, "state": "OPEN", "mark": None}]
    turn = bb.owner_counts(cards, items)
    assert (turn["needs_you"], turn["unlisted"]) == (1, 1)
    assert [it["bucket"] for it in items] == [bb.WAITING, bb.ABSENT]


def test_read_plan_index_keeps_lanes_without_a_queue(tmp_path):
    path = tmp_path / "chief-plan.json"
    assert read_plan_index(path)["state"] == "ok"  # no plan is a known answer
    path.write_text(json.dumps({"version": 1, "queue": [], "lanes": [
        {"repo": "app-launcher", "item": "#3", "status": "gate"}]}), encoding="utf-8")
    index = read_plan_index(path)
    assert index["state"] == "ok" and index["lanes"][0]["item"] == "#3"
    path.write_text("{not json", encoding="utf-8")
    assert read_plan_index(path)["state"] == "unreadable"
    path.write_text(json.dumps({"version": 2}), encoding="utf-8")
    assert read_plan_index(path)["state"] == "unreadable"
