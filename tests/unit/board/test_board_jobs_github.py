"""Board backend: jobs_attention and the gh-backed GitHub client."""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timedelta

from src import board
from src.clients import github as github_client

from tests.unit.board._support import _FakeGh, _seed_job


def test_jobs_attention_failed_today(webapp_client):
    _client, _app, overrides = webapp_client
    # run_job_cmd writes naive local ISO timestamps — mirror that exactly.
    # Anchored at local midday rather than the bare datetime.now() (#323): the
    # 1h/50min offsets below must never straddle a local-midnight calendar-day
    # boundary, which a bare `now()` does whenever the test happens to run in
    # the ~50 minutes after midnight. `now=local_now` is also passed explicitly
    # to jobs_attention() so the assertion is fully clock-independent, matching
    # how the rest of this file injects `now` into board functions.
    local_now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    _seed_job(overrides, "pipeline", {
        "status": "failed",
        "started_at": (local_now - timedelta(hours=1)).isoformat(timespec="seconds"),
        "finished_at": (local_now - timedelta(minutes=50)).isoformat(timespec="seconds"),
        "exit_code": 1,
    })
    cards = board.jobs_attention(now=local_now)
    assert [(c["job_id"], c["state"]) for c in cards] == [("pipeline", "failed")]


def test_jobs_attention_ignores_yesterdays_failure(webapp_client):
    _client, _app, overrides = webapp_client
    local_now = datetime.now()
    _seed_job(overrides, "old_fail", {
        "status": "failed",
        "finished_at": (local_now - timedelta(days=2)).isoformat(timespec="seconds"),
        "exit_code": 1,
    })
    assert board.jobs_attention() == []


def test_jobs_attention_unconfirmed_run_is_not_a_failed_card(webapp_client):
    """Issue #916: exit 122 is the scheduled-run adapter saying "this may well
    have delivered, but nobody established that". The card stays — an
    unverified run still wants a human's eye — but it must not be drawn as a
    failure, because a Board that cries failure on healthy runs is a Board
    whose failures stop being read."""
    _client, _app, overrides = webapp_client
    local_now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    _seed_job(overrides, "truncated", {
        "status": "failed",
        "started_at": (local_now - timedelta(hours=1)).isoformat(timespec="seconds"),
        "finished_at": (local_now - timedelta(minutes=50)).isoformat(timespec="seconds"),
        "exit_code": 122,
    })
    cards = board.jobs_attention(now=local_now)
    assert [(c["job_id"], c["state"]) for c in cards] == [
        ("truncated", "unconfirmed")
    ]
    # And it says why, so the card is actionable without a log dive.
    assert "never verified" in cards[0]["error"]


def test_jobs_attention_genuine_failure_still_reads_as_failed(webapp_client):
    """The other half of #916, and the one that makes the fix worth having:
    exit 123 is the run reporting it delivered no work. It must keep the
    failure rendering it had, or the false alarms are simply replaced by
    false comfort."""
    _client, _app, overrides = webapp_client
    local_now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    _seed_job(overrides, "delivered-nothing", {
        "status": "failed",
        "started_at": (local_now - timedelta(hours=1)).isoformat(timespec="seconds"),
        "finished_at": (local_now - timedelta(minutes=50)).isoformat(timespec="seconds"),
        "exit_code": 123,
    })
    cards = board.jobs_attention(now=local_now)
    assert [(c["job_id"], c["state"]) for c in cards] == [
        ("delivered-nothing", "failed")
    ]
    assert "delivered no work" in cards[0]["error"]


def test_jobs_attention_stuck_run(webapp_client):
    _client, _app, overrides = webapp_client
    local_now = datetime.now()
    # One lone running run, 30 min old: no p95 history, so the stuck floor
    # (300 s) applies and it counts as stuck.
    _seed_job(overrides, "wedged", {
        "status": "running",
        "started_at": (local_now - timedelta(minutes=30)).isoformat(timespec="seconds"),
    })
    cards = board.jobs_attention()
    assert [(c["job_id"], c["state"]) for c in cards] == [("wedged", "stuck")]


def test_jobs_attention_unreadable_history_surfaces_not_skipped(
    webapp_client, monkeypatch, caplog
):
    """#915: a job whose run history can't be read raises an ``unreadable``
    card instead of dropping out of the check, and says so above debug —
    once per job, not once per 5 s poll. A readable quiet job beside it
    still raises nothing, and a recovery clears the card."""
    from src import jobs as jobs_mod

    _client, _app, overrides = webapp_client
    local_now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    finished = (local_now - timedelta(minutes=50)).isoformat(timespec="seconds")
    _seed_job(overrides, "quiet", {"status": "success", "finished_at": finished})
    _seed_job(overrides, "locked", {"status": "success", "finished_at": finished})

    real_latest_run = jobs_mod.latest_run

    def _locked_latest_run(job_id):
        if job_id == "locked":
            raise PermissionError(13, "Access is denied", "run.json")
        return real_latest_run(job_id)

    monkeypatch.setattr(jobs_mod, "latest_run", _locked_latest_run)
    with caplog.at_level(logging.DEBUG, logger="src.board"):
        cards = board.jobs_attention(now=local_now)
        board.jobs_attention(now=local_now)  # the next poll must not re-warn

    assert [(c["job_id"], c["state"]) for c in cards] == [("locked", "unreadable")]
    assert "Access is denied" in cards[0]["error"]
    warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "locked" in r.getMessage()
    ]
    assert len(warnings) == 1

    monkeypatch.setattr(jobs_mod, "latest_run", real_latest_run)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="src.board"):
        assert board.jobs_attention(now=local_now) == []
    assert any("readable again" in r.getMessage() for r in caplog.records)


def test_github_refresh_and_snapshot(monkeypatch):
    fake = _FakeGh()
    monkeypatch.setattr(github_client.subprocess, "run", fake)
    snap = github_client.refresh("ferraroroberto")
    assert snap["error"] is None
    assert snap["fetched_at"]
    assert [i["number"] for i in snap["issues"]] == [164]
    assert snap["issues"][0]["repo"] == "app-launcher"
    assert snap["issues"][0]["labels"] == ["enhancement"]
    assert [p["number"] for p in snap["prs"]] == [67]
    assert snap["done"] == []
    # snapshot() is the memory read the poll uses — no new subprocess calls.
    calls_before = len(fake.calls)
    assert github_client.snapshot()["issues"] == snap["issues"]
    assert len(fake.calls) == calls_before
    assert all(argv[0] == "gh" for argv in fake.calls)

    # #1286: a throwaway instance swaps in its own gh through the env ...
    monkeypatch.setenv(github_client.GH_CMD_ENV, json.dumps(["py.exe", "fake_gh.py"]))
    fake.calls.clear()
    assert github_client.refresh("demo-owner")["error"] is None
    assert fake.calls and all(argv[:3] == ["py.exe", "fake_gh.py", "search"] for argv in fake.calls)
    # ... and a malformed value is an error, never a fall-back to the real gh.
    monkeypatch.setenv(github_client.GH_CMD_ENV, "gh")
    fake.calls.clear()
    assert github_client.refresh("demo-owner")["error"]
    assert fake.calls == []


def test_search_open_issues_filters_audit_meta_label(monkeypatch):
    """Ledger/metadata issues from ``/codebase-audit`` (label ``audit-meta``)
    are bookkeeping, not dispatchable work — the Board hides them."""
    actionable = {
        "repository": {"nameWithOwner": "ferraroroberto/voice-transcriber"},
        "number": 95, "title": "Usage analytics", "url": "u95",
        "updatedAt": "2026-07-02T10:00:00Z",
        "labels": [{"name": "enhancement"}],
    }
    ledger = {
        "repository": {"nameWithOwner": "ferraroroberto/voice-transcriber"},
        "number": 37, "title": "codebase-audit ledger", "url": "u37",
        "updatedAt": "2026-07-01T10:00:00Z",
        "labels": [{"name": "audit-meta"}],
    }

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps([actionable, ledger]), stderr=""
        )

    monkeypatch.setattr(github_client.subprocess, "run", fake_run)
    issues = github_client.search_open_issues("ferraroroberto")
    assert [i["number"] for i in issues] == [95]


def test_search_open_prs_filters_audit_meta_label(monkeypatch):
    actionable = {
        "repository": {"nameWithOwner": "ferraroroberto/app-launcher"},
        "number": 158, "title": "keyboard-aware overlay", "url": "u158",
        "updatedAt": "2026-07-02T09:00:00Z", "isDraft": False,
        "labels": [{"name": "bug"}],
    }
    ledger_pr = {
        "repository": {"nameWithOwner": "ferraroroberto/app-launcher"},
        "number": 200, "title": "audit ledger housekeeping", "url": "u200",
        "updatedAt": "2026-07-02T09:00:00Z", "isDraft": False,
        "labels": [{"name": "audit-meta"}],
    }

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps([actionable, ledger_pr]), stderr=""
        )

    monkeypatch.setattr(github_client.subprocess, "run", fake_run)
    prs = github_client.search_open_prs("ferraroroberto")
    assert [p["number"] for p in prs] == [158]


def test_done_today_filters_audit_meta_issue(monkeypatch):
    closed_ledger_issue = {
        "repository": {"nameWithOwner": "ferraroroberto/app-launcher"},
        "number": 37, "title": "codebase-audit ledger", "url": "u37",
        "updatedAt": "2026-07-02T14:00:00Z",
        "labels": [{"name": "audit-meta"}],
    }
    closed_real_issue = {
        "repository": {"nameWithOwner": "ferraroroberto/app-launcher"},
        "number": 9, "title": "closed by hand", "url": "u9",
        "updatedAt": "2026-07-02T14:30:00Z",
        "labels": [{"name": "bug"}],
    }

    def fake_run(argv, **kwargs):
        assert "prs" not in argv  # Done never fetches PRs (#399)
        rows = [closed_ledger_issue, closed_real_issue]
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(rows), stderr="")

    monkeypatch.setattr(github_client.subprocess, "run", fake_run)
    done = github_client.search_done_today("ferraroroberto")
    cards = {(d["kind"], d["repo"], d["number"]) for d in done}
    assert cards == {("issue", "app-launcher", 9)}


def test_done_today_is_closed_issues_only(monkeypatch):
    """Done holds closed issues only (#399) — no merged-PR fetch or pairing;
    a PR that closed an issue is already reflected by the issue's own card."""
    closed_issue = {
        "repository": {"nameWithOwner": "ferraroroberto/app-launcher"},
        "number": 305, "title": "status sticks", "url": "u305",
        "updatedAt": "2026-07-02T15:00:01Z",
    }

    def fake_run(argv, **kwargs):
        assert "prs" not in argv
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps([closed_issue]), stderr="")

    monkeypatch.setattr(github_client.subprocess, "run", fake_run)
    done = github_client.search_done_today("ferraroroberto")
    assert [(d["kind"], d["repo"], d["number"]) for d in done] == [("issue", "app-launcher", 305)]


def _closed_row(number, updated_at, closed_at=None):
    row = {
        "repository": {"nameWithOwner": "ferraroroberto/app-launcher"},
        "number": number, "title": f"closed {number}", "url": f"u{number}",
        "updatedAt": updated_at, "labels": [{"name": "bug"}],
    }
    if closed_at is not None:
        row["closedAt"] = closed_at
    return row


def test_done_today_asks_for_closed_at_and_sorts_by_it(monkeypatch):
    """#1638: an issue closed earlier but commented on later keeps its place
    below the one that closed after it; ``closedAt`` is requested and rides
    out as ``closed_at``."""
    seen = {}
    rows = [
        # Closed 13:40, commented on at 15:36 (updatedAt is the later one).
        _closed_row(1, "2026-10-15T15:36:00Z", "2026-10-15T13:40:00Z"),
        _closed_row(2, "2026-10-15T15:29:00Z", "2026-10-15T15:29:00Z"),
        _closed_row(3, "2026-10-15T09:00:00Z", "2026-10-15T09:00:00Z"),
    ]

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(rows), stderr="")

    monkeypatch.setattr(github_client.subprocess, "run", fake_run)
    done = github_client.search_done_today("ferraroroberto")
    fields = seen["argv"][seen["argv"].index("--json") + 1].split(",")
    assert "closedAt" in fields
    assert [d["number"] for d in done] == [2, 1, 3]
    assert done[1]["closed_at"] == "2026-10-15T13:40:00Z"


def test_done_today_falls_back_to_updated_at_without_a_close_time(monkeypatch):
    rows = [
        _closed_row(1, "2026-10-15T10:00:00Z", "2026-10-15T08:00:00Z"),
        _closed_row(2, "2026-10-15T12:00:00Z"),          # no closedAt
        _closed_row(3, "2026-10-15T11:00:00Z"),
    ]
    rows[2]["closedAt"] = None                            # null, same as missing

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(rows), stderr="")

    monkeypatch.setattr(github_client.subprocess, "run", fake_run)
    done = github_client.search_done_today("ferraroroberto")
    assert [d["number"] for d in done] == [2, 3, 1]
    assert done[0]["closed_at"] is None


def test_synthetic_gh_closed_rows_carry_closed_at():
    """#1638: the synthetic gh must emit ``closedAt`` or the new field reads
    as missing in every gate and design-review run."""
    from scripts import synthetic_gh

    rows = synthetic_gh.answer(["search", "issues", "--state", "closed"])
    assert rows and all(r.get("closedAt") for r in rows)
    open_rows = synthetic_gh.answer(["search", "issues", "--state", "open"])
    assert all("closedAt" not in r for r in open_rows)


def test_github_refresh_failure_keeps_old_data(monkeypatch):
    fake = _FakeGh()
    monkeypatch.setattr(github_client.subprocess, "run", fake)
    github_client.refresh("ferraroroberto")

    def _boom(argv, **kwargs):
        raise FileNotFoundError("gh not on PATH")

    monkeypatch.setattr(github_client.subprocess, "run", _boom)
    snap = github_client.refresh("ferraroroberto")
    assert "gh" in (snap["error"] or "")
    assert [i["number"] for i in snap["issues"]] == [164]  # previous data survives


def _malformed_gh(argv, **kwargs):
    """gh exits 0 but a row is not an object — the normalisers raise
    AttributeError, which is not a GhError."""
    return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(["not-a-row"]), stderr="")


def test_github_refresh_unexpected_failure_records_error(monkeypatch):
    """#910: *any* refresh failure leaves a non-null ``error`` for the next
    reader — not just GhError. Previously the exception escaped refresh()
    and the cache kept ``error: None``, so nothing recorded the failure."""
    monkeypatch.setattr(github_client.subprocess, "run", _FakeGh())
    github_client.refresh("ferraroroberto")

    monkeypatch.setattr(github_client.subprocess, "run", _malformed_gh)
    snap = github_client.refresh("ferraroroberto")
    assert "AttributeError" in (snap["error"] or "")
    assert [i["number"] for i in snap["issues"]] == [164]  # previous data survives
    assert github_client.snapshot()["error"] == snap["error"]  # survives to the next read


def test_api_refresh_unexpected_failure_surfaces_on_next_board_read(
    webapp_client, monkeypatch
):
    """#910: the failure reaches the API — the refresh answers with the
    error instead of a 500, and the following poll still carries it."""
    client, _app, _overrides = webapp_client
    monkeypatch.setattr(github_client.subprocess, "run", _malformed_gh)

    github = client.post("/api/board/github/refresh").json()
    assert github["available"] is False
    assert "AttributeError" in (github["error"] or "")

    body = client.get("/api/board").json()
    assert body["github"]["error"] == github["error"]
    assert body["github"]["available"] is False
