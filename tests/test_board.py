"""Board tab (issue #300 / #164) — server-side logic + API shape.

Covers the three sources and their degradation contract:
  * ``board.read_sessions_state`` — absent / corrupt / fresh / stale files.
  * ``board.merge_sessions`` — the normalized-cwd join (equal + subdir), the
    two-sessions-one-dir recency tie-break, state-only cards, unknown fallback.
  * ``board.jobs_attention`` — failed-today and stuck runs from run.json trees.
  * ``src.github_client`` — canned ``gh`` JSON via a monkeypatched
    ``subprocess.run``; missing binary → error surfaced, old data kept.
  * ``GET /api/board`` + ``POST /api/board/github/refresh`` via the standard
    ``webapp_client`` fixture (session-host mocked, config in tmp).
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import board, github_client


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _pristine_gh_cache():
    """The gh cache is module-global — every test starts and ends empty."""
    github_client.reset_cache()
    yield
    github_client.reset_cache()


# ------------------------------------------------------ read_sessions_state


def test_state_missing_file_unavailable(tmp_path: Path):
    result = board.read_sessions_state(tmp_path / "nope.json", now=NOW)
    assert result == {"available": False, "stale": False, "updated_at": None, "rows": {}}


def test_state_corrupt_file_unavailable(tmp_path: Path):
    target = tmp_path / "sessions-state.json"
    target.write_text("{not json", encoding="utf-8")
    assert board.read_sessions_state(target, now=NOW)["available"] is False


def test_state_fresh_rows(tmp_path: Path):
    target = tmp_path / "sessions-state.json"
    target.write_text(json.dumps({
        "sid-1": {"project": "photo-ocr", "status": "needs-you",
                  "cwd": "E:/automation/photo-ocr",
                  "updated_at": _iso(NOW - timedelta(minutes=12))},
    }), encoding="utf-8")
    result = board.read_sessions_state(target, now=NOW)
    assert result["available"] is True
    assert result["stale"] is False
    assert set(result["rows"]) == {"sid-1"}


def test_state_stale_when_newest_row_old(tmp_path: Path):
    target = tmp_path / "sessions-state.json"
    target.write_text(json.dumps({
        "sid-1": {"status": "working", "cwd": "x",
                  "updated_at": _iso(NOW - timedelta(hours=30))},
    }), encoding="utf-8")
    assert board.read_sessions_state(target, now=NOW)["stale"] is True


# ------------------------------------------------------------ merge_sessions


def _live(session_id: str, project_dir: str, started_min_ago: int, **extra):
    row = {
        "session_id": session_id,
        "kind": "pty",
        "agent": "claude",
        "project_dir": project_dir,
        "name": Path(project_dir).name,
        "alive": True,
        "started_at": _iso(NOW - timedelta(minutes=started_min_ago)),
        "live_title": "",
        "prompt_title": "",
    }
    row.update(extra)
    return row


def _state_row(cwd: str, status: str = "working", updated_min_ago: int = 5, **extra):
    row = {
        "project": Path(cwd).name,
        "status": status,
        "transcript_path": None,
        "cwd": cwd,
        "updated_at": _iso(NOW - timedelta(minutes=updated_min_ago)),
    }
    row.update(extra)
    return row


def test_merge_joins_by_normalized_cwd():
    cards = board.merge_sessions(
        [_live("aaa", "E:\\automation\\photo-ocr", 30)],
        {"t-uuid": _state_row("e:/automation/photo-ocr", status="needs-you")},
        now=NOW,
    )
    assert len(cards) == 1
    assert cards[0]["status"] == "needs-you"
    assert cards[0]["project"] == "photo-ocr"
    assert cards[0]["session_id"] == "aaa"


def test_merge_matches_cwd_under_project_dir():
    cards = board.merge_sessions(
        [_live("aaa", "E:/automation/app-launcher", 30)],
        {"t": _state_row("E:/automation/app-launcher/subdir", status="working")},
        now=NOW,
    )
    assert cards[0]["status"] == "working"


def test_merge_two_sessions_one_dir_recency_tiebreak():
    older = _live("old", "E:/automation/app-launcher", 120)
    newer = _live("new", "E:/automation/app-launcher", 5)
    cards = board.merge_sessions(
        [older, newer],
        {"t": _state_row("E:/automation/app-launcher", status="needs-you")},
        now=NOW,
    )
    by_id = {c["session_id"]: c for c in cards}
    assert by_id["new"]["status"] == "needs-you"
    assert by_id["old"]["status"] == "unknown"


def test_merge_live_without_state_is_unknown():
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 10)], {}, now=NOW)
    assert cards[0]["status"] == "unknown"
    assert cards[0]["project"] == "y"


def test_merge_bad_status_is_unknown():
    cards = board.merge_sessions(
        [_live("aaa", "E:/x/y", 10)],
        {"t": _state_row("E:/x/y", status="exploded")},
        now=NOW,
    )
    assert cards[0]["status"] == "unknown"


def test_merge_fresh_state_only_row_becomes_external_card():
    cards = board.merge_sessions(
        [], {"t": _state_row("E:/automation/reporting", status="needs-you")}, now=NOW
    )
    assert len(cards) == 1
    assert cards[0]["alive"] is False
    assert cards[0]["session_id"] is None
    assert cards[0]["kind"] == "external"
    assert cards[0]["status"] == "needs-you"


def test_merge_cold_state_only_row_dropped():
    cards = board.merge_sessions(
        [], {"t": _state_row("E:/x", updated_min_ago=60 * 30)}, now=NOW
    )
    assert cards == []


# --------------------------------- working-ghost drop (#322)


def test_merge_working_ghost_dropped(tmp_path: Path):
    """A headless/sdk-cli row stuck at 'working' with a long-quiet transcript
    is a dead process, not active work — dropped, not rendered."""
    row = _state_row("E:/automation/local-llm-hub", status="working", updated_min_ago=40)
    row["transcript_path"] = _transcript_file(tmp_path, NOW - timedelta(minutes=40))
    cards = board.merge_sessions([], {"t": row}, now=NOW)
    assert cards == []


def test_merge_working_fresh_transcript_still_renders(tmp_path: Path):
    """A genuinely active 'working' row (recent transcript activity) still
    shows up — the ghost check must not catch real work in progress."""
    row = _state_row("E:/automation/local-llm-hub", status="working", updated_min_ago=40)
    row["transcript_path"] = _transcript_file(tmp_path, NOW - timedelta(minutes=1))
    cards = board.merge_sessions([], {"t": row}, now=NOW)
    assert len(cards) == 1
    assert cards[0]["status"] == "working"


def test_merge_working_no_transcript_path_still_renders():
    """No transcript_path at all → nothing to check staleness against; keep
    the existing (pre-#322) behavior of trusting the hook status."""
    row = _state_row("E:/automation/local-llm-hub", status="working", updated_min_ago=40)
    cards = board.merge_sessions([], {"t": row}, now=NOW)
    assert len(cards) == 1
    assert cards[0]["status"] == "working"


def test_merge_needs_you_quiet_transcript_still_renders(tmp_path: Path):
    """The ghost check is scoped to 'working' only — a quiet transcript on a
    needs-you row is the expected shape of a real waiting session."""
    row = _state_row("E:/automation/local-llm-hub", status="needs-you", updated_min_ago=40)
    row["transcript_path"] = _transcript_file(tmp_path, NOW - timedelta(minutes=40))
    cards = board.merge_sessions([], {"t": row}, now=NOW)
    assert len(cards) == 1
    assert cards[0]["status"] == "needs-you"


# ------------------------------- transcript activity overlay (#305 / #309)


def _msg_line(kind: str, ts: datetime) -> str:
    """One real conversation line — the shape the #309 tail probe accepts."""
    return json.dumps({
        "type": kind,
        "timestamp": _iso(ts),
        "message": {"role": kind, "content": [{"type": "text", "text": "hi"}]},
    })


def _transcript_file(tmp_path: Path, mtime: datetime, content: str = None) -> str:
    """A transcript file with its mtime pinned to ``mtime``.

    Default content is a single assistant line stamped at ``mtime``, so mtime
    and last-activity agree (the plain #305 shape). Pass ``content`` to make
    them diverge — the #309 metadata-only-appends case.
    """
    target = tmp_path / "transcript.jsonl"
    if content is None:
        content = _msg_line("assistant", mtime) + "\n"
    target.write_text(content, encoding="utf-8")
    stamp = mtime.timestamp()
    os.utime(target, (stamp, stamp))
    return str(target)


def test_overlay_needs_you_with_active_transcript_is_working(tmp_path: Path):
    """Resume paths fire no hook (#305): a transcript appended well after the
    row's stamp means Claude is working, whatever the last hook event said."""
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, NOW - timedelta(minutes=1))
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "working"
    # The age re-anchors to the transcript activity, not the stale hook stamp.
    assert cards[0]["age_seconds"] == 60


def test_overlay_idle_with_active_transcript_is_working(tmp_path: Path):
    row = _state_row("E:/x/y", status="idle", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, NOW - timedelta(minutes=1))
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "working"


def test_overlay_inside_stop_epsilon_keeps_needs_you(tmp_path: Path):
    """Stop's row stamp and the final transcript write land seconds apart (in
    either order) — inside the epsilon the hook status wins, so a genuine
    needs-you alert is never delayed."""
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=10) + timedelta(seconds=5)
    )
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "needs-you"


def test_overlay_missing_transcript_keeps_hook_status(tmp_path: Path):
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = str(tmp_path / "gone.jsonl")
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "needs-you"


def test_overlay_applies_to_external_cards(tmp_path: Path):
    row = _state_row("E:/automation/reporting", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, NOW - timedelta(minutes=1))
    cards = board.merge_sessions([], {"t": row}, now=NOW)
    assert cards[0]["kind"] == "external"
    assert cards[0]["status"] == "working"


def test_overlay_metadata_only_appends_keep_needs_you(tmp_path: Path):
    """#309: post-Stop metadata lines (system, pr-link, snapshots) advance the
    file mtime past the epsilon with no real resume — the tail probe sees the
    last conversation line is still pre-stamp and keeps the hook status."""
    stamp_time = NOW - timedelta(minutes=10)
    content = (
        _msg_line("assistant", stamp_time - timedelta(seconds=3)) + "\n"
        + json.dumps({"type": "system",
                      "timestamp": _iso(stamp_time + timedelta(minutes=2))}) + "\n"
        + json.dumps({"type": "pr-link", "url": "https://x"}) + "\n"
        + json.dumps({"type": "file-history-snapshot", "snapshot": {"f": 1}}) + "\n"
    )
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=1), content
    )
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "needs-you"
    # Age still anchors to the hook stamp, not the metadata mtime.
    assert cards[0]["age_seconds"] == 600


def test_overlay_metadata_only_appends_keep_idle(tmp_path: Path):
    stamp_time = NOW - timedelta(minutes=10)
    content = (
        _msg_line("assistant", stamp_time - timedelta(seconds=3)) + "\n"
        + json.dumps({"type": "ai-title", "title": "t"}) + "\n"
    )
    row = _state_row("E:/x/y", status="idle", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=1), content
    )
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "idle"


def test_overlay_message_buried_under_metadata_still_flips(tmp_path: Path):
    """A real resume followed by metadata lines: the reverse walk skips the
    metadata and finds the conversation line, so the flip still happens."""
    resumed = NOW - timedelta(minutes=1)
    content = (
        _msg_line("user", resumed) + "\n"
        + json.dumps({"type": "file-history-snapshot", "snapshot": {}}) + "\n"
    )
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, resumed, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "working"
    assert cards[0]["age_seconds"] == 60


def test_overlay_malformed_tail_keeps_hook_status(tmp_path: Path):
    """Unparseable tail (torn write, junk) degrades to the hook status."""
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=1), "{torn line no json\nnot json either\n"
    )
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "needs-you"


# ------------------------------------------------------------ jobs_attention


def _seed_job(overrides: dict, job_id: str, run: dict) -> None:
    jobs_path = overrides["tmp_jobs_path"]
    existing = json.loads(jobs_path.read_text(encoding="utf-8")) if jobs_path.exists() else {"jobs": []}
    existing["jobs"].append({
        "id": job_id,
        "name": job_id.replace("_", " "),
        "script_path": "C:/nowhere/script.py",
    })
    jobs_path.write_text(json.dumps(existing), encoding="utf-8")

    run_id = run.get("run_id", "20260702T090000")
    run_dir = overrides["tmp_jobs_runs_dir"] / job_id / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps({"run_id": run_id, "job_id": job_id, **run}), encoding="utf-8"
    )


def test_jobs_attention_failed_today(webapp_client):
    _client, _app, overrides = webapp_client
    # run_job_cmd writes naive local ISO timestamps — mirror that exactly.
    local_now = datetime.now()
    _seed_job(overrides, "pipeline", {
        "status": "failed",
        "started_at": (local_now - timedelta(hours=1)).isoformat(timespec="seconds"),
        "finished_at": (local_now - timedelta(minutes=50)).isoformat(timespec="seconds"),
        "exit_code": 1,
    })
    cards = board.jobs_attention()
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


# ------------------------------------------------------------- github_client


_CANNED_ISSUE = {
    "repository": {"nameWithOwner": "ferraroroberto/app-launcher"},
    "number": 164, "title": "Board tab", "url": "https://github.com/x/164",
    "updatedAt": "2026-07-01T10:00:00Z",
    "labels": [{"name": "enhancement"}],
}
_CANNED_PR = {
    "repository": {"nameWithOwner": "ferraroroberto/photo-ocr"},
    "number": 67, "title": "fix chunk merge", "url": "https://github.com/x/67",
    "updatedAt": "2026-07-02T08:00:00Z", "isDraft": False,
}


class _FakeGh:
    """subprocess.run stand-in keyed on the gh subcommand + filters."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        rows = []
        if "prs" in argv and "--merged" in argv:
            rows = []                      # nothing merged today
        elif "prs" in argv:
            rows = [_CANNED_PR]
        elif "issues" in argv and "closed" in argv:
            rows = []                      # nothing closed today
        elif "issues" in argv:
            rows = [_CANNED_ISSUE]
        completed = subprocess.CompletedProcess(argv, 0, stdout=json.dumps(rows), stderr="")
        return completed


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


def test_done_pairs_closed_issue_with_merging_pr(monkeypatch):
    """One card per unit of work (#301 phone feedback): an issue closed by a
    merged PR ("Closes #N" in its body) folds into the PR card; issues
    closed by hand keep their own card; cross-repo refs don't pair here."""
    merged_pr = {
        "repository": {"nameWithOwner": "ferraroroberto/app-launcher"},
        "number": 306, "title": "fix: overlay", "url": "u306",
        "updatedAt": "2026-07-02T15:00:00Z",
        "body": "## Summary\n\nFixes the thing.\n\nCloses #305. "
                "Also fixes other-repo#77 (their card, not ours).",
    }
    closed_paired = {
        "repository": {"nameWithOwner": "ferraroroberto/app-launcher"},
        "number": 305, "title": "status sticks", "url": "u305",
        "updatedAt": "2026-07-02T15:00:01Z",
    }
    closed_alone = {
        "repository": {"nameWithOwner": "ferraroroberto/reporting"},
        "number": 9, "title": "closed by hand", "url": "u9",
        "updatedAt": "2026-07-02T14:00:00Z",
    }

    def fake_run(argv, **kwargs):
        rows = [merged_pr] if "--merged" in argv else [closed_paired, closed_alone]
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(rows), stderr="")

    monkeypatch.setattr(github_client.subprocess, "run", fake_run)
    done = github_client.search_done_today("ferraroroberto")
    cards = {(d["kind"], d["repo"], d["number"]) for d in done}
    assert ("pr", "app-launcher", 306) in cards
    assert ("issue", "reporting", 9) in cards
    assert ("issue", "app-launcher", 305) not in cards
    pr = next(d for d in done if d["kind"] == "pr")
    assert pr["closes"] == [305]


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


# ---------------------------------------------------------------- API shape


def test_api_board_shape_with_everything_absent(webapp_client):
    client, _app, _overrides = webapp_client
    body = client.get("/api/board").json()
    assert set(body["columns"]) == {"backlog", "claude_turn", "your_turn", "done"}
    assert body["github"] == {"fetched_at": None, "error": None}
    assert body["sessions_state"]["available"] is False
    assert body["columns"]["backlog"] == []
    assert body["generated_at"]


def test_api_board_merges_live_sessions_and_state(webapp_client):
    client, app, overrides = webapp_client
    overrides["session"].list_sessions.return_value = [
        _live("live-1", "E:/automation/photo-ocr", 20),
    ]
    state_file = Path(app.state.webapp_config.sessions_state_file)
    state_file.write_text(json.dumps({
        "t-uuid": {"project": "photo-ocr", "status": "needs-you",
                   "cwd": "E:/automation/photo-ocr",
                   "updated_at": _iso(datetime.now(timezone.utc) - timedelta(minutes=3))},
    }), encoding="utf-8")

    body = client.get("/api/board").json()
    assert body["sessions_state"]["available"] is True
    your_turn = body["columns"]["your_turn"]
    assert [c["session_id"] for c in your_turn] == ["live-1"]
    assert your_turn[0]["status"] == "needs-you"
    assert body["columns"]["claude_turn"] == []


def test_api_board_survives_session_host_down(webapp_client):
    client, _app, overrides = webapp_client
    from src.session_client import SessionHostError
    overrides["session"].list_sessions.side_effect = SessionHostError("down")
    body = client.get("/api/board").json()
    assert body["columns"]["claude_turn"] == []


def test_api_refresh_endpoint_fills_cache(webapp_client, monkeypatch):
    client, _app, _overrides = webapp_client
    fake = _FakeGh()
    monkeypatch.setattr(github_client.subprocess, "run", fake)

    github = client.post("/api/board/github/refresh").json()
    assert github["error"] is None
    assert github["fetched_at"]
    # The owner from config reaches the gh command line.
    assert any("testowner" in " ".join(argv) for argv in fake.calls)

    body = client.get("/api/board").json()
    assert [c["number"] for c in body["columns"]["backlog"]] == [164]
    assert [c["kind"] for c in body["columns"]["your_turn"]] == ["pr"]
