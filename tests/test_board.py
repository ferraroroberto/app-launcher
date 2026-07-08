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


# -------------------------------------------------------- read_rate_limits


_EMPTY_RATE_LIMITS = {
    "available": False, "stale": False, "updated_at": None,
    "five_hour": None, "seven_day": None,
}


def test_rate_limits_missing_file_unavailable(tmp_path: Path):
    result = board.read_rate_limits(tmp_path / "nope.json", now=NOW)
    assert result == _EMPTY_RATE_LIMITS


def test_rate_limits_corrupt_file_unavailable(tmp_path: Path):
    target = tmp_path / "rate-limits.json"
    target.write_text("{not json", encoding="utf-8")
    assert board.read_rate_limits(target, now=NOW)["available"] is False


def test_rate_limits_fresh_both_windows(tmp_path: Path):
    target = tmp_path / "rate-limits.json"
    target.write_text(json.dumps({
        "five_hour": {"used_percentage": 42, "resets_at": 1751640000},
        "seven_day": {"used_percentage": 77, "resets_at": 1751900000},
        "captured_at": _iso(NOW - timedelta(minutes=2)),
    }), encoding="utf-8")
    result = board.read_rate_limits(target, now=NOW)
    assert result["available"] is True
    assert result["stale"] is False
    assert result["five_hour"] == {"used_percentage": 42, "resets_at": 1751640000}
    assert result["seven_day"] == {"used_percentage": 77, "resets_at": 1751900000}


def test_rate_limits_stale_when_captured_at_old(tmp_path: Path):
    target = tmp_path / "rate-limits.json"
    target.write_text(json.dumps({
        "five_hour": {"used_percentage": 10, "resets_at": 1751640000},
        "captured_at": _iso(NOW - timedelta(minutes=45)),
    }), encoding="utf-8")
    assert board.read_rate_limits(target, now=NOW)["stale"] is True


def test_rate_limits_one_window_absent(tmp_path: Path):
    target = tmp_path / "rate-limits.json"
    target.write_text(json.dumps({
        "five_hour": {"used_percentage": 10, "resets_at": 1751640000},
        "captured_at": _iso(NOW),
    }), encoding="utf-8")
    result = board.read_rate_limits(target, now=NOW)
    assert result["five_hour"] == {"used_percentage": 10, "resets_at": 1751640000}
    assert result["seven_day"] is None


def test_rate_limits_window_is_null(tmp_path: Path):
    target = tmp_path / "rate-limits.json"
    target.write_text(json.dumps({
        "five_hour": None,
        "seven_day": {"used_percentage": 5, "resets_at": 1751900000},
        "captured_at": _iso(NOW),
    }), encoding="utf-8")
    result = board.read_rate_limits(target, now=NOW)
    assert result["five_hour"] is None
    assert result["seven_day"] == {"used_percentage": 5, "resets_at": 1751900000}


def test_rate_limits_tolerates_utf8_bom(tmp_path: Path):
    # The fleet-config statusline writer is PowerShell; .NET's
    # [System.Text.Encoding]::UTF8 defaults to emitting a BOM (fleet-config#259
    # found this the hard way). A BOM'd file must still parse, not read as
    # corrupt.
    target = tmp_path / "rate-limits.json"
    payload = json.dumps({
        "five_hour": {"used_percentage": 41.7, "resets_at": 1751640000},
        "captured_at": _iso(NOW),
    })
    target.write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))
    result = board.read_rate_limits(target, now=NOW)
    assert result["available"] is True
    assert result["five_hour"] == {"used_percentage": 41.7, "resets_at": 1751640000}


def test_rate_limits_window_present_but_null_fields(tmp_path: Path):
    target = tmp_path / "rate-limits.json"
    target.write_text(json.dumps({
        "five_hour": {"used_percentage": None, "resets_at": None},
        "captured_at": _iso(NOW),
    }), encoding="utf-8")
    result = board.read_rate_limits(target, now=NOW)
    assert result["available"] is True
    assert result["five_hour"] == {"used_percentage": None, "resets_at": None}


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


def test_merge_claimed_card_carries_state_sid():
    """#307: the card threads through the claimed row's own key, so a Slack
    ping (which only knows this transcript UUID) can resolve to the card."""
    cards = board.merge_sessions(
        [_live("aaa", "E:/automation/photo-ocr", 30)],
        {"t-uuid": _state_row("E:/automation/photo-ocr", status="needs-you")},
        now=NOW,
    )
    assert cards[0]["state_sid"] == "t-uuid"


def test_merge_unclaimed_live_session_state_sid_is_none():
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 10)], {}, now=NOW)
    assert cards[0]["state_sid"] is None


# ----------------------------------------- shared session title (#396)


def test_merge_carries_shared_name_from_state_row():
    """A matched state row's name/name_source (fleet-config#302) rides onto
    the card as shared_name/shared_name_source, alongside the existing
    live_title/prompt_title fields — the Coding tab reads the identical
    fields via board.attach_shared_names()."""
    cards = board.merge_sessions(
        [_live("aaa", "E:/automation/photo-ocr", 30)],
        {"t": _state_row(
            "E:/automation/photo-ocr", status="needs-you",
            name="Fixing the chunk merge bug",
        )},
        now=NOW,
    )
    assert cards[0]["shared_name"] == "Fixing the chunk merge bug"
    assert cards[0]["shared_name_source"] is None


def test_merge_carries_shared_name_source_derived():
    cards = board.merge_sessions(
        [_live("aaa", "E:/automation/photo-ocr", 30)],
        {"t": _state_row(
            "E:/automation/photo-ocr", status="working",
            name="photo-ocr-2", name_source="derived",
        )},
        now=NOW,
    )
    assert cards[0]["shared_name"] == "photo-ocr-2"
    assert cards[0]["shared_name_source"] == "derived"


def test_merge_no_state_row_shared_name_is_none():
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 10)], {}, now=NOW)
    assert cards[0]["shared_name"] is None
    assert cards[0]["shared_name_source"] is None


def test_merge_state_only_card_carries_shared_name():
    cards = board.merge_sessions(
        [], {"t": _state_row(
            "E:/automation/reporting", status="needs-you", name="Recap run",
        )}, now=NOW,
    )
    assert cards[0]["shared_name"] == "Recap run"


# --------------------------------------------------- attach_shared_names


def test_attach_shared_names_joins_by_cwd():
    live = [_live("aaa", "E:/automation/photo-ocr", 30)]
    state_rows = {"t": _state_row(
        "E:/automation/photo-ocr", name="Chunk merge fix",
    )}
    joined = board.attach_shared_names(live, state_rows)
    assert len(joined) == 1
    assert joined[0]["shared_name"] == "Chunk merge fix"
    assert joined[0]["shared_name_source"] is None
    # Every original field survives the join.
    assert joined[0]["session_id"] == "aaa"
    assert joined[0]["project_dir"] == "E:/automation/photo-ocr"


def test_attach_shared_names_no_match_returns_none():
    live = [_live("aaa", "E:/x/y", 10)]
    joined = board.attach_shared_names(live, {})
    assert joined[0]["shared_name"] is None
    assert joined[0]["shared_name_source"] is None


def test_attach_shared_names_does_not_mutate_input():
    live = [_live("aaa", "E:/automation/photo-ocr", 30)]
    state_rows = {"t": _state_row("E:/automation/photo-ocr", name="X")}
    board.attach_shared_names(live, state_rows)
    assert "shared_name" not in live[0]


def test_attach_shared_names_agrees_with_merge_sessions():
    """The Coding tab and Board tab must resolve the same live session to the
    same title source — same cwd claim walk, same result (#396 acceptance)."""
    live = [_live("aaa", "E:/automation/photo-ocr", 30)]
    state_rows = {"t": _state_row(
        "E:/automation/photo-ocr", status="needs-you", name="Chunk merge fix",
    )}
    coding_tab = board.attach_shared_names(live, state_rows)
    board_tab = board.merge_sessions(live, state_rows, now=NOW)
    assert coding_tab[0]["shared_name"] == board_tab[0]["shared_name"]
    assert coding_tab[0]["shared_name_source"] == board_tab[0]["shared_name_source"]


def test_merge_fresh_state_only_row_becomes_external_card():
    cards = board.merge_sessions(
        [], {"t": _state_row("E:/automation/reporting", status="needs-you")}, now=NOW
    )
    assert len(cards) == 1
    assert cards[0]["alive"] is False
    assert cards[0]["session_id"] is None
    assert cards[0]["kind"] == "external"
    assert cards[0]["status"] == "needs-you"
    # #307: state-only cards have no session-host id and no drawer target —
    # deep-linking to them is out of scope, so they carry no state_sid.
    assert cards[0].get("state_sid") is None


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
    assert set(body["columns"]) == {"backlog", "claude_turn", "your_turn", "other", "done"}
    assert body["github"] == {"fetched_at": None, "error": None}
    assert body["sessions_state"]["available"] is False
    assert body["rate_limits"]["available"] is False
    assert body["columns"]["backlog"] == []
    assert body["generated_at"]


def test_api_board_rate_limits_present(webapp_client):
    client, app, _overrides = webapp_client
    rate_limits_file = Path(app.state.webapp_config.rate_limits_file)
    rate_limits_file.write_text(json.dumps({
        "five_hour": {"used_percentage": 42, "resets_at": 1751640000},
        "seven_day": {"used_percentage": 88, "resets_at": 1751900000},
        "captured_at": _iso(datetime.now(timezone.utc) - timedelta(minutes=1)),
    }), encoding="utf-8")

    body = client.get("/api/board").json()
    assert body["rate_limits"]["available"] is True
    assert body["rate_limits"]["stale"] is False
    assert body["rate_limits"]["five_hour"] == {"used_percentage": 42, "resets_at": 1751640000}
    assert body["rate_limits"]["seven_day"] == {"used_percentage": 88, "resets_at": 1751900000}


def test_api_rate_limits_standalone_endpoint_absent(webapp_client):
    client, _app, _overrides = webapp_client
    body = client.get("/api/rate-limits").json()
    assert body == {
        "available": False, "stale": False, "updated_at": None,
        "five_hour": None, "seven_day": None,
    }


def test_api_rate_limits_standalone_endpoint_present(webapp_client):
    client, app, _overrides = webapp_client
    rate_limits_file = Path(app.state.webapp_config.rate_limits_file)
    rate_limits_file.write_text(json.dumps({
        "five_hour": {"used_percentage": 10, "resets_at": 1751640000},
        "captured_at": _iso(datetime.now(timezone.utc)),
    }), encoding="utf-8")

    body = client.get("/api/rate-limits").json()
    assert body["available"] is True
    assert body["five_hour"] == {"used_percentage": 10, "resets_at": 1751640000}
    assert body["seven_day"] is None


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
    assert body["columns"]["your_turn"] == []
    assert [c["kind"] for c in body["columns"]["other"]] == ["pr"]
