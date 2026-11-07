"""Backlog buckets: one fixture per precedence rule, plus the unknown states."""

from __future__ import annotations

import json

import pytest

from src.board import backlog_buckets as bb
from src.chief.plan import read_plan_index


def _card(number, labels=(), in_progress=False):
    return {"kind": "issue", "repo": "app-launcher", "number": number,
            "labels": list(labels), "in_progress": in_progress}


def _plan(lanes=(), queue=(), waiting=()):
    return {"state": "ok", "lanes": list(lanes), "queue": list(queue), "waiting": list(waiting)}


def _row(n, status="queued", **extra):
    return {"repo": "app-launcher", "ref": f"#{n}", "status": status, "kind": "",
            "blocked_by": "", "after": "", **extra}


def _place(card, plan, markers_read=True, records=()):
    health = bb.classify([card], list(records), plan, markers_read=markers_read)
    return card["bucket"], card["bucket_chips"], health


@pytest.mark.parametrize("card,plan,bucket,chips", [
    # 1. In progress: a marker, or a lane, beats every lower rule.
    (_card(1, ["needs-owner"], in_progress=True), _plan(), bb.IN_PROGRESS, []),
    (_card(1, ["needs-validation"]),
     _plan(lanes=[{"repo": "app-launcher", "item": "#1", "status": "gate"}], queue=[_row(1)]),
     bb.IN_PROGRESS, ["gate"]),
    # 2. Waiting on you: each source on its own.
    (_card(2, ["needs-owner"]), _plan(queue=[_row(2)]), bb.WAITING, ["before start"]),
    (_card(2, ["Needs-Validation"]), _plan(), bb.WAITING, ["validate"]),
    (_card(2), _plan(waiting=[{"ref": "app-launcher#2", "repo": "app-launcher"}]), bb.WAITING, ["question"]),
    (_card(2), _plan(queue=[_row(2, "waiting-roberto")]), bb.WAITING, ["question"]),
    (_card(2), _plan(queue=[_row(2, "parked", kind="owner")]), bb.WAITING, ["owner step"]),
    # 3. Queued, with its reasons; building with no lane or marker stays queued.
    (_card(3), _plan(queue=[_row(3)]), bb.QUEUED, []),
    (_card(3), _plan(queue=[_row(3, "building")]), bb.QUEUED, ["building"]),
    (_card(3), _plan(queue=[_row(3, after="fleet-config#9 no-lane:fleet-config")]),
     bb.QUEUED, ["after fleet-config#9", "after no lane in fleet-config"]),
    (_card(3), _plan(queue=[_row(3, "parked", kind="blocked-by", blocked_by="life-os#4")]),
     bb.QUEUED, ["parked", "blocked by life-os#4"]),
    # Umbrella rows fold.
    (_card(5), _plan(queue=[_row(5, "parked", kind="umbrella")]), bb.FOLDED, ["umbrella"]),
    # 4. Not planned; a row for another repo's #4 does not match.
    (_card(4), _plan(queue=[{**_row(4), "repo": "life-os"}]), bb.NOT_PLANNED, []),
])
def test_precedence(card, plan, bucket, chips):
    got_bucket, got_chips, _ = _place(card, plan)
    assert (got_bucket, got_chips) == (bucket, chips)


def test_unreadable_plan_is_unknown_never_not_planned():
    plan = {"state": "unreadable", "lanes": [], "queue": [], "waiting": []}
    card = _card(7, ["needs-owner"])
    bucket, _, health = _place(card, plan)
    assert bucket == bb.UNKNOWN
    assert health == {"plan": "unreadable", "markers": "ok", "unknown_reason": "plan unreadable"}
    held = _card(8, in_progress=True)
    assert _place(held, plan)[0] == bb.IN_PROGRESS


def test_unreadable_markers_are_unknown_but_a_lane_still_places():
    bucket, _, health = _place(_card(7), _plan(), markers_read=False)
    assert bucket == bb.UNKNOWN and health["unknown_reason"] == "issue markers unreadable"
    lane = _plan(lanes=[{"repo": "app-launcher", "item": "#7", "status": ""}])
    assert _place(_card(7), lane, markers_read=False)[:2] == (bb.IN_PROGRESS, ["lane"])


def test_every_issue_lands_in_exactly_one_bucket_and_records_fold():
    cards = [_card(n) for n in range(1, 6)]
    record = {"kind": "issue", "repo": "app-launcher", "number": 99, "labels": ["audit-meta"]}
    plan = _plan(queue=[_row(2), _row(3, "parked", kind="umbrella")])
    bb.classify(cards, [record], plan, markers_read=True)
    assert [c["bucket"] for c in cards] == [
        bb.NOT_PLANNED, bb.QUEUED, bb.FOLDED, bb.NOT_PLANNED, bb.NOT_PLANNED]
    assert (record["bucket"], record["bucket_chips"]) == (bb.FOLDED, ["record"])


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
