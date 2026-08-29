"""Board tab (issue #300 / #164) — server-side logic + API shape.

Covers the three sources and their degradation contract:
  * ``board.read_sessions_state`` — absent / corrupt / fresh / stale files.
  * ``board.read_active_issues`` — absent / corrupt / fresh / expired markers.
  * ``board.merge_sessions`` — agent-aware state claims (exact launcher id,
    normalized-cwd fallback), external-card freshness, unknown fallback.
  * ``board.jobs_attention`` — failed-today and stuck runs from run.json trees.
  * ``src.github_client`` — canned ``gh`` JSON via a monkeypatched
    ``subprocess.run``; missing binary → error surfaced, old data kept.
  * ``GET /api/board`` + ``POST /api/board/github/refresh`` via the standard
    ``webapp_client`` fixture (session-host mocked, config in tmp).
"""

from __future__ import annotations

import json
import logging
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


# ------------------------------------------------------ read_active_issues


def test_active_issues_missing_or_corrupt_file_unavailable(tmp_path: Path):
    target = tmp_path / "active-issues.json"
    assert board.read_active_issues(target, now=NOW) == {
        "available": False, "updated_at": None, "rows": {},
    }
    target.write_text("{not json", encoding="utf-8")
    assert board.read_active_issues(target, now=NOW)["available"] is False


def test_active_issues_keeps_fresh_and_expires_orphans(tmp_path: Path):
    target = tmp_path / "active-issues.json"
    target.write_text(json.dumps({
        "APP-LAUNCHER#528": {
            "repo": "app-launcher", "number": 528,
            "branch": "feat/528-active", "started_at": _iso(NOW - timedelta(hours=2)),
        },
        "photo-ocr#73": {
            "repo": "photo-ocr", "number": 73,
            "branch": "fix/73-old", "started_at": _iso(NOW - timedelta(hours=25)),
        },
        "broken#1": {"repo": "broken", "number": 1, "started_at": "garbage"},
    }), encoding="utf-8")

    result = board.read_active_issues(target, now=NOW)
    assert result["available"] is True
    assert result["updated_at"] == _iso(NOW - timedelta(hours=2))
    assert set(result["rows"]) == {"app-launcher#528"}


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
    # #608: the raw hook "needs-you" is split; no transcript to check
    # pending tool_use against, so it lands on the safe generic fallback.
    assert cards[0]["status"] == "awaiting-input"
    assert cards[0]["project"] == "photo-ocr"
    assert cards[0]["session_id"] == "aaa"


def test_merge_carries_label_from_live_session():
    """The role tag (#245) rides the live session into the board card so the
    frontend can single out e.g. the fleet chief; absent → empty string."""
    cards = board.merge_sessions(
        [
            _live("aaa", "E:\\automation\\fleet-config", 30, label="chief"),
            _live("bbb", "E:\\automation\\photo-ocr", 20),
        ],
        {},
        now=NOW,
    )
    by_id = {c["session_id"]: c for c in cards}
    assert by_id["aaa"]["label"] == "chief"
    assert by_id["bbb"]["label"] == ""


def test_merge_matches_cwd_under_project_dir():
    cards = board.merge_sessions(
        [_live("aaa", "E:/automation/app-launcher", 30)],
        {"t": _state_row("E:/automation/app-launcher/subdir", status="working")},
        now=NOW,
    )
    assert cards[0]["status"] == "working"


def test_merge_two_sessions_one_dir_ambiguous_row_stays_unclaimed():
    """#537: 2+ live sessions sharing one cwd can't be safely disambiguated
    by recency alone — the old greedy newest-wins tiebreak was exactly the
    mechanism that cross-wired live Board cards to the wrong session's
    transcript. Neither session claims the single row; both render
    ``unknown`` instead of one confidently (and possibly wrongly)
    inheriting it."""
    older = _live("old", "E:/automation/app-launcher", 120)
    newer = _live("new", "E:/automation/app-launcher", 5)
    cards = board.merge_sessions(
        [older, newer],
        {"t": _state_row("E:/automation/app-launcher", status="needs-you")},
        now=NOW,
    )
    by_id = {c["session_id"]: c for c in cards}
    assert by_id["new"]["status"] == "unknown"
    assert by_id["old"]["status"] == "unknown"


def test_merge_three_sessions_one_dir_never_cross_wires_transcripts():
    """#537 repro: 3 live sessions sharing one project directory, none
    carrying ``launcher_session_id`` (the exact-match path is unavailable —
    true for externally-started sessions, and for any launcher-spawned
    session predating the env-var propagation fix). Reproduced live against
    the real matching code: the old greedy recency tiebreak assigned every
    one of 3 such sessions a DIFFERENT session's transcript, all wrong
    simultaneously. No card may show another session's transcript_path —
    the safe outcome here is every card staying unmatched (``unknown``,
    no transcript), not a confident wrong guess."""
    sessions = [
        _live("s-oldest", "E:/automation/app-launcher", 60),
        _live("s-middle", "E:/automation/app-launcher", 40),
        _live("s-newest", "E:/automation/app-launcher", 20),
    ]
    rows = {
        "row-a": _state_row(
            "E:/automation/app-launcher", status="needs-you",
            updated_min_ago=1, transcript_path="a.jsonl",
        ),
        "row-b": _state_row(
            "E:/automation/app-launcher", status="working",
            updated_min_ago=5, transcript_path="b.jsonl",
        ),
        "row-c": _state_row(
            "E:/automation/app-launcher", status="needs-you",
            updated_min_ago=10, transcript_path="c.jsonl",
        ),
    }
    cards = board.merge_sessions(sessions, rows, now=NOW)
    by_id = {c["session_id"]: c for c in cards}
    assert len(by_id) == 3
    for sid in ("s-oldest", "s-middle", "s-newest"):
        assert by_id[sid]["status"] == "unknown"


def test_merge_non_claude_does_not_claim_legacy_claude_row():
    """#455: legacy rows are Claude rows. A Codex session in the same cwd
    must stay truthful/unknown instead of borrowing Claude's needs-you state."""
    codex = _live(
        "codex-live", "E:/automation/app-launcher", 5, agent="codex"
    )
    cards = board.merge_sessions(
        [codex],
        {"claude-transcript": _state_row(
            "E:/automation/app-launcher", status="needs-you", updated_min_ago=240
        )},
        now=NOW,
    )
    assert len(cards) == 1
    assert cards[0]["session_id"] == "codex-live"
    assert cards[0]["agent"] == "codex"
    assert cards[0]["status"] == "unknown"
    assert cards[0]["state_sid"] is None


def test_merge_exact_launcher_id_beats_same_cwd_recency():
    """Agent writers can provide launcher_session_id for a deterministic
    claim; a newer sibling row in the same cwd must not steal the session."""
    live = [_live("launcher-aaa", "E:/automation/app-launcher", 30)]
    state_rows = {
        "right": _state_row(
            "E:/automation/app-launcher", status="needs-you",
            updated_min_ago=20, agent="claude",
            launcher_session_id="launcher-aaa",
        ),
        "newer-other": _state_row(
            "E:/automation/app-launcher", status="working",
            updated_min_ago=1, agent="claude",
            launcher_session_id="launcher-bbb",
        ),
    }
    cards = board.merge_sessions(live, state_rows, now=NOW)
    assert len(cards) == 1
    # #608 split; see test_merge_joins_by_normalized_cwd.
    assert cards[0]["status"] == "awaiting-input"
    assert cards[0]["state_sid"] == "right"


def test_merge_duplicate_exact_launcher_ids_take_newest_row():
    """A resume/transcript rollover may leave more than one hook row carrying
    the inherited launcher id; exact identity still needs a deterministic
    newest-event tie-break."""
    live = [_live("launcher-aaa", "E:/automation/app-launcher", 30)]
    state_rows = {
        "older": _state_row(
            "E:/automation/app-launcher", status="working",
            updated_min_ago=20, agent="claude",
            launcher_session_id="launcher-aaa",
        ),
        "newer": _state_row(
            "E:/automation/app-launcher", status="needs-you",
            updated_min_ago=1, agent="claude",
            launcher_session_id="launcher-aaa",
        ),
    }
    cards = board.merge_sessions(live, state_rows, now=NOW)
    # #608 split; see test_merge_joins_by_normalized_cwd.
    assert cards[0]["status"] == "awaiting-input"
    assert cards[0]["state_sid"] == "newer"


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


def test_merge_cwd_fallback_ignores_row_older_than_session_start():
    """#482: a brand-new live session with no exact-id row yet must not
    inherit an unrelated leftover same-cwd row's title. The leftover here was
    last updated hours before this session even started, so it can only be
    some earlier, unrelated conversation in the same directory — a row can
    only be a session's own state if it was written at-or-after that
    session's started_at."""
    live = [_live("new-session", "E:/automation/app-launcher", 2)]
    state_rows = {
        "stale-leftover": _state_row(
            "E:/automation/app-launcher", status="needs-you",
            updated_min_ago=300, name="Vendor project-scaffolding's button component",
        ),
    }
    cards = board.merge_sessions(live, state_rows, now=NOW)
    assert cards[0]["shared_name"] is None
    assert cards[0]["status"] == "unknown"
    assert cards[0]["state_sid"] is None


# ------------------------------------------ manual title override (#458)


def test_merge_carries_manual_title_from_live_session():
    """The session-host's manual_title (a launcher-native rename) rides onto
    the card alongside live_title/prompt_title — the Board drawer's rename
    button reads/writes the same field the Coding tab does."""
    cards = board.merge_sessions(
        [_live("aaa", "E:/automation/photo-ocr", 30, manual_title="my rename")],
        {}, now=NOW,
    )
    assert cards[0]["manual_title"] == "my rename"


def test_merge_no_manual_title_defaults_empty_string():
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 10)], {}, now=NOW)
    assert cards[0]["manual_title"] == ""


def test_merge_external_card_manual_title_is_empty_string(tmp_path: Path):
    """State-only (external) cards have no live session to hold an
    override — always empty, never None (matches live_title/prompt_title)."""
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, NOW - timedelta(minutes=1))
    cards = board.merge_sessions([], {"t": row}, now=NOW)
    assert cards[0]["kind"] == "external"
    assert cards[0]["manual_title"] == ""


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


def test_attach_shared_names_does_not_cross_agent_boundary():
    live = [_live("codex-live", "E:/automation/app-launcher", 10, agent="codex")]
    state_rows = {"claude-row": _state_row(
        "E:/automation/app-launcher", name="Claude title",
    )}
    joined = board.attach_shared_names(live, state_rows)
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


def test_merge_unverifiable_state_only_row_is_suppressed():
    """#455: a hook row without a live host match or an existing, recently
    active transcript is not proof that a process exists."""
    cards = board.merge_sessions(
        [], {"t": _state_row("E:/automation/reporting", status="needs-you")}, now=NOW
    )
    assert cards == []


def test_merge_cold_state_only_row_dropped():
    cards = board.merge_sessions(
        [], {"t": _state_row("E:/x", updated_min_ago=60 * 30)}, now=NOW
    )
    assert cards == []


# ----------------------- external-state liveness (#322 / #455)


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


def test_merge_working_no_transcript_path_is_suppressed():
    """#455: missing cloud/bridge transcripts are no longer trusted as
    external process-liveness evidence for up to 24 hours."""
    row = _state_row("E:/automation/local-llm-hub", status="working", updated_min_ago=40)
    cards = board.merge_sessions([], {"t": row}, now=NOW)
    assert cards == []


def test_merge_needs_you_quiet_transcript_is_suppressed(tmp_path: Path):
    """A waiting hook state is semantic evidence, not process-liveness
    evidence; once its external transcript is quiet, the card must clear."""
    row = _state_row("E:/automation/local-llm-hub", status="needs-you", updated_min_ago=40)
    row["transcript_path"] = _transcript_file(tmp_path, NOW - timedelta(minutes=40))
    cards = board.merge_sessions([], {"t": row}, now=NOW)
    assert cards == []


# ---------------------------------- ghost / dead-process cards (#613) -----


def test_merge_shared_transcript_superseded_row_does_not_double_render(tmp_path: Path):
    """#613 acceptance test: the fleet-config ghost, reproduced. A worker
    moving from one issue to the next gets its row re-keyed — the superseded
    row stays unmatched but keeps pointing at the transcript the live session
    keeps writing. One live session must yield exactly one card, never a
    real one plus a ghost carrying the old row's stale status."""
    transcript = _transcript_file(tmp_path, NOW - timedelta(minutes=1))
    live = [_live("aaa", "E:/automation/fleet-config", 30)]
    state_rows = {
        "current-row": _state_row(
            "E:/automation/fleet-config", status="working", updated_min_ago=1,
            transcript_path=transcript, launcher_session_id="aaa",
        ),
        "superseded-row": _state_row(
            "E:/automation/fleet-config", status="working", updated_min_ago=30,
            transcript_path=transcript, launcher_session_id="aaa",
        ),
    }
    cards = board.merge_sessions(live, state_rows, now=NOW)
    assert len(cards) == 1
    assert cards[0]["kind"] == "pty"
    assert cards[0]["session_id"] == "aaa"


def test_merge_reaped_launcher_session_suppressed_despite_fresh_transcript(tmp_path: Path):
    """#613: a hook row whose own ``launcher_session_id`` no longer appears
    in the live session-host list is provably dead (the session-host is the
    authority on which PTYs it owns) -- suppressed regardless of how recently
    its transcript happened to be touched. The Codex ghost's shape: a
    launcher-spawned session hard-killed without a SessionEnd, its row still
    inside the transcript-freshness window."""
    row = _state_row(
        "E:/automation/app-launcher", status="needs-you", updated_min_ago=5,
        transcript_path=_transcript_file(tmp_path, NOW - timedelta(minutes=1)),
        launcher_session_id="dead-pty-id",
    )
    cards = board.merge_sessions([], {"t": row}, now=NOW)
    assert cards == []


def test_merge_reaped_launcher_session_with_no_transcript_also_suppressed():
    row = _state_row(
        "E:/automation/app-launcher", status="working", updated_min_ago=2,
        launcher_session_id="dead-pty-id",
    )
    cards = board.merge_sessions([], {"t": row}, now=NOW)
    assert cards == []


def test_merge_reaped_codex_launcher_session_suppressed(tmp_path: Path):
    """#613's literal reported ghost, reproduced: ``agent=codex``,
    ``status=needs-you``, ~14.3 min old -- the exact shape from the issue.
    The reaped-session check is agent-agnostic (keyed only on
    ``launcher_session_id``, never on ``agent``), and a Codex row spawned via
    the launcher's own session-host does carry ``launcher_session_id``
    (fleet-config#349's ``session_state_codex.py`` inherits
    ``APP_LAUNCHER_SESSION_ID`` from the same PTY env Claude rows use) -- so
    this ghost is suppressed exactly like the Claude case above, regardless
    of how fresh its transcript looks."""
    row = _state_row(
        "E:/automation/app-launcher", status="needs-you", updated_min_ago=14,
        transcript_path=_transcript_file(tmp_path, NOW - timedelta(minutes=1)),
        launcher_session_id="dead-codex-pty-id", agent="codex",
    )
    cards = board.merge_sessions([], {"t": row}, now=NOW)
    assert cards == []


def test_external_row_liveness_reaped_check_does_not_fire_when_id_is_live(tmp_path: Path):
    """Unit-level check on ``_external_row_liveness`` directly: the
    reaped-session check must not fire for a row whose ``launcher_session_id``
    genuinely IS in the live set -- it falls through to the transcript-claim
    check and then the ordinary freshness fallback, exactly as before #613."""
    from src.board_transcript import _external_row_liveness

    row = _state_row(
        "E:/automation/app-launcher", status="working", updated_min_ago=2,
        transcript_path=_transcript_file(tmp_path, NOW - timedelta(minutes=1)),
        launcher_session_id="still-alive",
    )
    live_ok, reason = _external_row_liveness(
        row, NOW,
        claimed_transcripts=set(),
        live_launcher_session_ids={"still-alive"},
    )
    assert live_ok is True
    assert reason == "recent transcript activity"


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
    needs-you alert is never delayed. #608: the default fixture transcript
    is a plain text reply with nothing pending, so the split correctly reads
    it as a clean stop, not just an undifferentiated needs-you."""
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=10) + timedelta(seconds=5)
    )
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "idle-finished"


def test_live_title_is_busy_glyph_range():
    from src.board_transcript import _live_title_is_busy

    assert _live_title_is_busy("⠂ chief") is True  # a real observed spinner frame
    assert _live_title_is_busy("⠐ Start work on issue 635") is True  # another frame
    assert _live_title_is_busy("✳ Some Conversation Title") is False  # idle marker
    assert _live_title_is_busy("") is False
    assert _live_title_is_busy(None) is False
    assert _live_title_is_busy("   ") is False
    assert _live_title_is_busy("app-launcher | gpt-5.5") is False  # Codex folder-echo title
    # The glyph must lead the title, not appear mid-string.
    assert _live_title_is_busy("Some Title ⠂") is False


def test_live_title_is_busy_matches_the_circle_quadrant_spinner():
    """#815: the CLI moved its spinner from Braille Patterns to the
    circle-quadrant rotation, which this check did not recognize — so every
    live working session read as not-busy and the whole #631 override went
    inert. Both observed frames, plus the two remaining frames of that
    4-frame rotation carried as a deliberate superset."""
    from src.board_transcript import _live_title_is_busy

    assert _live_title_is_busy("\u25d0 Issue add") is True  # observed live
    assert _live_title_is_busy("\u25d1 Issue add") is True  # observed live
    assert _live_title_is_busy("\u25d2 x") is True
    assert _live_title_is_busy("\u25d3 x") is True
    # The idle marker sits between the two busy families and must stay out.
    assert _live_title_is_busy("\u2733 chief") is False


def test_unrecognized_title_glyph_logs_one_breadcrumb_per_glyph(caplog):
    """#815's real lesson was that the fall-through is silent: the spinner
    changed, the override died, and nothing said so. An unrecognized glyph
    lead now leaves exactly one info line naming the codepoint — once per
    distinct glyph, since the Board re-polls every 5s — while ASCII titles
    and the known idle marker stay quiet, and the verdict is unchanged."""
    from src.board_transcript import (
        _LOGGED_UNKNOWN_TITLE_GLYPHS,
        _live_title_is_busy,
    )

    _LOGGED_UNKNOWN_TITLE_GLYPHS.clear()
    with caplog.at_level(logging.INFO, logger="src.board_transcript"):
        for _ in range(3):
            assert _live_title_is_busy("\u2699 future spinner") is False
        assert _live_title_is_busy("\u2733 chief") is False
        assert _live_title_is_busy("app-launcher | gpt-5.5") is False
        assert _live_title_is_busy("") is False
    breadcrumbs = [r for r in caplog.records if "unrecognized glyph" in r.getMessage()]
    assert len(breadcrumbs) == 1
    assert "U+2699" in breadcrumbs[0].getMessage()
    _LOGGED_UNKNOWN_TITLE_GLYPHS.clear()


def test_overlay_circle_spinner_title_overrides_stale_needs_you(tmp_path: Path):
    """#815 end-to-end: the exact production shape from #813 — a needs-you
    row whose session is genuinely mid-turn, carrying today's circle-quadrant
    spinner — must resolve to working off the PTY signal alone, without the
    transcript deciding it. Same fixture as #631's Braille test."""
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=10) + timedelta(seconds=3)
    )
    cards = board.merge_sessions(
        [_live(
            "aaa", "E:/x/y", 30,
            live_title="\u25d0 Issue add",
            last_output_at=(NOW - timedelta(seconds=1)).timestamp(),
        )],
        {"t": row},
        now=NOW,
    )
    assert cards[0]["status"] == "working"


def test_overlay_wedged_circle_spinner_title_does_not_force_working(tmp_path: Path):
    """#636's freshness gate must apply to the new glyph family too — a
    circle spinner frozen on a PTY that has gone quiet is stale chrome, not
    proof of a live turn, and still falls through to the transcript."""
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=10) + timedelta(seconds=3)
    )
    cards = board.merge_sessions(
        [_live(
            "aaa", "E:/x/y", 30,
            live_title="\u25d0 Issue add",
            last_output_at=(NOW - timedelta(minutes=5)).timestamp(),
        )],
        {"t": row},
        now=NOW,
    )
    assert cards[0]["status"] == "idle-finished"


def test_overlay_busy_live_title_overrides_stale_needs_you_inside_epsilon(tmp_path: Path):
    """#631: the classifier's tail-scan can land in the gap between a
    streamed text block and its own following tool_use block — landing
    inside the resume epsilon, with the transcript tail ending on a
    text-only assistant message, previously resolved to idle-finished even
    though a turn was genuinely still in progress. A busy live_title (the
    session's own OSC-title spinner glyph) is sourced from the live PTY, not
    this transcript file, so it short-circuits straight to working before
    the epsilon/tail-scan logic ever runs."""
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=10) + timedelta(seconds=3)
    )
    cards = board.merge_sessions(
        [_live("aaa", "E:/x/y", 30, live_title="\u2802 doing the thing")],
        {"t": row},
        now=NOW,
    )
    assert cards[0]["status"] == "working"
    assert cards[0]["age_seconds"] == 0


def test_overlay_idle_live_title_does_not_force_a_false_override(tmp_path: Path):
    """The idle marker ("*", U+2733) must not be mistaken for the busy
    spinner range (U+2800-U+28FF) — same epsilon scenario as the busy-title
    test above, but with the idle glyph, must keep the original outcome."""
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=10) + timedelta(seconds=3)
    )
    cards = board.merge_sessions(
        [_live("aaa", "E:/x/y", 30, live_title="\u2733 Some Conversation Title")],
        {"t": row},
        now=NOW,
    )
    assert cards[0]["status"] == "idle-finished"


# ------------------------------------------- wedged-PTY freshness gate (#636)


def test_pty_output_is_fresh_boundaries():
    from src.board_transcript import _WEDGED_PTY_AFTER, _pty_output_is_fresh

    assert _pty_output_is_fresh(None, NOW) is True  # no signal -> trust the glyph
    assert _pty_output_is_fresh(0.0, NOW) is True  # never recorded, same as no signal
    fresh = (NOW - timedelta(seconds=1)).timestamp()
    assert _pty_output_is_fresh(fresh, NOW) is True
    at_threshold = (NOW - _WEDGED_PTY_AFTER).timestamp()
    assert _pty_output_is_fresh(at_threshold, NOW) is True  # inclusive boundary
    stale = (NOW - _WEDGED_PTY_AFTER - timedelta(seconds=1)).timestamp()
    assert _pty_output_is_fresh(stale, NOW) is False


def test_overlay_busy_live_title_with_fresh_last_output_at_still_overrides(tmp_path: Path):
    """#636 must not regress #631: a busy live_title backed by genuinely
    recent PTY output (last_output_at inside the wedged-PTY threshold) keeps
    short-circuiting straight to working, same fixture as #631's own
    regression test."""
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=10) + timedelta(seconds=3)
    )
    cards = board.merge_sessions(
        [_live(
            "aaa", "E:/x/y", 30,
            live_title="\u2802 doing the thing",
            last_output_at=(NOW - timedelta(seconds=1)).timestamp(),
        )],
        {"t": row},
        now=NOW,
    )
    assert cards[0]["status"] == "working"


def test_overlay_wedged_busy_live_title_does_not_force_working(tmp_path: Path):
    """A busy live_title frozen on a PTY that has gone genuinely quiet (#636,
    #627's remainder) must not be trusted at face value — same fixture as
    the #631 busy-title regression test above, but with a stale
    last_output_at, must fall through to the ordinary transcript-based
    outcome instead of blindly reporting working."""
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=10) + timedelta(seconds=3)
    )
    cards = board.merge_sessions(
        [_live(
            "aaa", "E:/x/y", 30,
            live_title="\u2802 doing the thing",
            last_output_at=(NOW - timedelta(minutes=5)).timestamp(),
        )],
        {"t": row},
        now=NOW,
    )
    assert cards[0]["status"] != "working"
    assert cards[0]["status"] == "idle-finished"


def test_overlay_wedged_busy_live_title_falls_through_to_stalled_dispatch(tmp_path: Path):
    """A wedged busy live_title on top of a genuinely stalled background
    dispatch (#608) must resolve to stalled, not working — the freshness
    gate falling through has to reach the existing stalled-dispatch check,
    not just any transcript-based outcome. Same transcript fixture as
    test_stalled_background_dispatch_over_threshold_is_stalled."""
    stamp_time = NOW - timedelta(minutes=40)
    launch_time = NOW - timedelta(minutes=35)
    content = _tool_result_line(
        launch_time, {"stdout": "", "stderr": "", "backgroundTaskId": "btask1"}
    ) + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=40)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions(
        [_live(
            "aaa", "E:/x/y", 45,
            live_title="\u2802 doing the thing",
            last_output_at=(NOW - timedelta(minutes=5)).timestamp(),
        )],
        {"t": row},
        now=NOW,
    )
    assert cards[0]["status"] == "stalled"


def test_idle_finished_downgraded_when_repo_has_active_issue(tmp_path: Path):
    """#627: a clean stop with nothing pending is only an *absence* of
    evidence, not proof the session's own work is done — the same shape a
    turn cut off mid-task leaves behind. When the session's repo still has
    an open issue workflow (fleet-config's active-issue marker, already
    fetched every poll for the Backlog column), that ``idle-finished`` claim
    is downgraded to the safe ``awaiting-input`` rather than asserted from
    silence alone."""
    row = _state_row("E:/automation/app-launcher", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=10) + timedelta(seconds=5)
    )
    cards = board.merge_sessions(
        [_live("aaa", "E:/automation/app-launcher", 30)],
        {"t": row},
        now=NOW,
        active_issue_repos={"app-launcher"},
    )
    assert cards[0]["status"] == "awaiting-input"


def test_idle_finished_stays_finished_without_matching_active_issue(tmp_path: Path):
    """The downgrade is repo-scoped, not a blanket suppression — a session in
    a repo with no open issue workflow (or none at all) still reports
    ``idle-finished`` as before."""
    row = _state_row("E:/automation/app-launcher", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=10) + timedelta(seconds=5)
    )
    cards = board.merge_sessions(
        [_live("aaa", "E:/automation/app-launcher", 30)],
        {"t": row},
        now=NOW,
        active_issue_repos={"some-other-repo"},
    )
    assert cards[0]["status"] == "idle-finished"

    cards_no_repos = board.merge_sessions(
        [_live("aaa", "E:/automation/app-launcher", 30)], {"t": row}, now=NOW,
    )
    assert cards_no_repos[0]["status"] == "idle-finished"


def test_idle_finished_downgrade_matches_worktree_sibling(tmp_path: Path):
    """A session built in an isolated sibling worktree (``<repo>-wt-<N>``,
    ``skills/_lib/worktree_claim.py``) must resolve to the same repo as its
    primary checkout, or its own still-open issue marker would never match."""
    row = _state_row(
        "E:/automation/app-launcher-wt-627", status="needs-you", updated_min_ago=10
    )
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=10) + timedelta(seconds=5)
    )
    cards = board.merge_sessions(
        [_live("aaa", "E:/automation/app-launcher-wt-627", 30)],
        {"t": row},
        now=NOW,
        active_issue_repos={"app-launcher"},
    )
    assert cards[0]["status"] == "awaiting-input"


def test_idle_finished_downgrade_applies_to_external_cards(tmp_path: Path):
    """The same repo-scoped downgrade applies to unmatched/external rows
    (:func:`src.board_sessions.merge_sessions`'s second, no-live-match loop),
    not just live-session-claimed cards. ``updated_min_ago=2`` keeps the
    transcript mtime inside both the #305 resume epsilon (so the status
    doesn't flip to ``working``) and #613's 5-minute external-liveness
    freshness window (so the row still renders as a card at all)."""
    row = _state_row("E:/automation/reporting", status="needs-you", updated_min_ago=2)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=2) + timedelta(seconds=3)
    )
    cards = board.merge_sessions(
        [], {"t": row}, now=NOW, active_issue_repos={"reporting"},
    )
    assert cards[0]["kind"] == "external"
    assert cards[0]["status"] == "awaiting-input"


def test_active_issue_repos_lowercases_and_dedupes():
    rows = {
        "App-Launcher#627": {"repo": "App-Launcher", "number": 627},
        "app-launcher#628": {"repo": "app-launcher", "number": 628},
        "reporting#12": {"repo": "reporting", "number": 12},
        "bad-row": {"number": 1},
    }
    assert board.active_issue_repos(rows) == frozenset({"app-launcher", "reporting"})


def test_overlay_missing_transcript_keeps_hook_status(tmp_path: Path):
    """#608: a transcript path that can't be read gives no usable signal —
    the split must not misread that as "checked, nothing pending" and claim
    idle-finished; it falls back to the safe, generic value."""
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = str(tmp_path / "gone.jsonl")
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "awaiting-input"


def test_overlay_applies_to_external_cards(tmp_path: Path):
    row = _state_row("E:/automation/reporting", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, NOW - timedelta(minutes=1))
    cards = board.merge_sessions([], {"t": row}, now=NOW)
    assert cards[0]["kind"] == "external"
    assert cards[0]["status"] == "working"


def test_overlay_metadata_only_appends_keep_needs_you(tmp_path: Path):
    """#309: post-Stop metadata lines (system, pr-link, snapshots) advance the
    file mtime past the epsilon with no real resume — the tail probe sees the
    last conversation line is still pre-stamp and keeps the hook status.
    #608: the only real conversation line is a plain text reply with no
    tool_use pending, so the split correctly reads it as a clean stop."""
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
    assert cards[0]["status"] == "idle-finished"
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
    """Unparseable tail (torn write, junk) degrades to the hook status. #608:
    no line parsed at all is "no usable signal", not "checked, clean" — the
    split must not read this as idle-finished."""
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, NOW - timedelta(minutes=1), "{torn line no json\nnot json either\n"
    )
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "awaiting-input"


# --------------------------------- pending background dispatch overlay (#464)


def _tool_result_line(ts: datetime, result: dict) -> str:
    """One tool_result line whose ``toolUseResult`` carries a background
    dispatch's synchronous launch ack — the shape ``_launched_background_ids``
    reads. ``toolUseResult`` is a sibling of ``message``, not inside it, per
    live transcripts."""
    return json.dumps({
        "type": "user",
        "timestamp": _iso(ts),
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_x", "content": "ack"}],
        },
        "toolUseResult": result,
    })


def _task_notification_line(ts: datetime, task_id: str) -> str:
    """A completion notice in Claude Code's real ``queue-operation`` shape —
    invisible to ``_last_activity`` since it carries no ``message`` field.
    Real transcripts pair the ``enqueue`` with an almost-immediate ``dequeue``
    once Claude actually receives it (#601); every caller here wants the
    fully-delivered case, so that pairing is baked in — the enqueue-only,
    still-queued case has its own dedicated fixture below."""
    enqueue = json.dumps({
        "type": "queue-operation",
        "operation": "enqueue",
        "timestamp": _iso(ts),
        "content": (
            f"<task-notification>\n<task-id>{task_id}</task-id>\n"
            "<status>completed</status>\n</task-notification>"
        ),
    })
    dequeue = json.dumps({"type": "queue-operation", "operation": "dequeue", "timestamp": _iso(ts)})
    return enqueue + "\n" + dequeue


def test_overlay_pending_background_bash_keeps_working(tmp_path: Path):
    """A backgrounded Bash command Claude is still waiting on must not render
    as needs-you just because its own turn already ended — the parent
    transcript stays quiet the whole time it's running, so this must not
    depend on the mtime-past-stamp activity check."""
    stamp_time = NOW - timedelta(minutes=10)
    content = _tool_result_line(
        stamp_time, {"stdout": "", "stderr": "", "backgroundTaskId": "btask1"}
    ) + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "working"


def test_overlay_pending_background_agent_keeps_working(tmp_path: Path):
    """Same as above for an async sub-agent (Agent/Task tool) dispatch."""
    stamp_time = NOW - timedelta(minutes=10)
    content = _tool_result_line(
        stamp_time,
        {"isAsync": True, "status": "async_launched", "agentId": "agent1"},
    ) + "\n"
    row = _state_row("E:/x/y", status="idle", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "working"


def test_overlay_pending_dispatch_survives_large_intervening_tool_result(tmp_path: Path):
    """#594: a background dispatch's launch line can be pushed well past the
    old 8 KiB activity window by one large intervening tool result (e.g. a
    file Read) before the turn ends — reproduced from a live transcript where
    the launch line sat 11.8 KB from EOF. The wider ``_EXCHANGE_TAIL_BYTES``
    scan must still reach it."""
    stamp_time = NOW - timedelta(minutes=10)
    launch = _tool_result_line(
        stamp_time, {"stdout": "", "stderr": "", "backgroundTaskId": "btask1"}
    ) + "\n"
    # Padding well past the old 8 KiB window but inside the new 256 KiB one.
    filler = (json.dumps({"type": "system", "content": "x" * 400}) + "\n") * 30
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, launch + filler)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "working"


def _turn_duration_line(ts: datetime, pending_agent_count: int) -> str:
    """Claude Code's own end-of-turn accounting line, emitted the moment a
    turn ends — the harness-native signal
    :func:`src.board_transcript._pending_background_dispatch_launched_at`
    falls back to when its id-keyed scan comes up empty (#627)."""
    return json.dumps({
        "type": "system",
        "subtype": "turn_duration",
        "durationMs": 1000,
        "pendingBackgroundAgentCount": pending_agent_count,
        "timestamp": _iso(ts),
    })


def test_overlay_pending_dispatch_beyond_tail_window_uses_turn_duration_count(tmp_path: Path):
    """#627, reproduced from a live transcript (project-scaffolding#199's
    worker): a background ``Agent`` dispatch's launch line sat ~299 KB from
    EOF by the time its turn ended — past even the widened 256 KiB
    ``_EXCHANGE_TAIL_BYTES`` window (#594 already widened it once, from 8 KB,
    for the same class of problem). The id-keyed scan below found nothing
    outstanding, so the row read as a clean stop (``idle-finished``) while the
    dispatch was still genuinely running — exactly the false-negative #627
    warns is worse than the five prior false-``needs-you`` reports it
    replaced. Claude Code's own ``turn_duration`` line reports
    ``pendingBackgroundAgentCount`` independent of any byte window (the real
    transcript's matching line read ``1`` at the same instant); the scan must
    fall back to it when the id-keyed search finds nothing."""
    stamp_time = NOW - timedelta(minutes=10)
    launch = _tool_result_line(
        stamp_time, {"isAsync": True, "status": "async_launched", "agentId": "agent1"}
    ) + "\n"
    # Padding well past the 256 KiB _EXCHANGE_TAIL_BYTES window.
    filler = (json.dumps({"type": "system", "content": "x" * 400}) + "\n") * 700
    final_text = _msg_line("assistant", stamp_time) + "\n"
    turn_duration = _turn_duration_line(stamp_time, 1) + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, stamp_time, launch + filler + final_text + turn_duration
    )
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "working"


def test_overlay_turn_duration_zero_pending_does_not_block_idle_finished(tmp_path: Path):
    """#627: a ``turn_duration`` line reporting zero pending agents is not
    itself evidence of outstanding work — a genuinely clean stop must still
    read as idle-finished, turn_duration line or not."""
    stamp_time = NOW - timedelta(minutes=10)
    final_text = _msg_line("assistant", stamp_time) + "\n"
    turn_duration = _turn_duration_line(stamp_time, 0) + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(
        tmp_path, stamp_time, final_text + turn_duration
    )
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "idle-finished"


def test_overlay_background_dispatch_notified_keeps_needs_you(tmp_path: Path):
    """Once the completion notice lands, a genuine needs-you-family status
    still wins — the pending check must not keep matching after the work
    actually finished. #608: nothing else is pending in this fixture, so the
    split reads it as a clean stop."""
    stamp_time = NOW - timedelta(minutes=10)
    content = (
        _tool_result_line(
            stamp_time - timedelta(minutes=1),
            {"stdout": "", "stderr": "", "backgroundTaskId": "btask1"},
        ) + "\n"
        + _task_notification_line(stamp_time - timedelta(seconds=30), "btask1") + "\n"
    )
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "idle-finished"


def test_overlay_background_dispatch_enqueued_not_dequeued_keeps_working(tmp_path: Path):
    """#601: a live transcript (app-launcher#601, home-automation issue #321)
    ended with the notification's ``enqueue`` line as the very last thing
    written — no ``dequeue``/``remove`` followed it anywhere in the tail, so
    Claude had not actually received the result yet. The notification being
    merely *ready* (enqueued) must not resolve the dispatch — only being
    handed to Claude (dequeued) may."""
    stamp_time = NOW - timedelta(minutes=10)
    content = (
        _tool_result_line(
            stamp_time - timedelta(minutes=1),
            {"stdout": "", "stderr": "", "backgroundTaskId": "btask1"},
        ) + "\n"
        + json.dumps({
            "type": "queue-operation",
            "operation": "enqueue",
            "timestamp": _iso(stamp_time - timedelta(seconds=30)),
            "content": (
                "<task-notification>\n<task-id>btask1</task-id>\n"
                "<status>completed</status>\n</task-notification>"
            ),
        }) + "\n"
    )
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "working"


# ------------------------ pending background dispatch, tool-use-id keyed (#576)


def _bash_tool_use_line(
    ts: datetime, tool_use_id: str, *, run_in_background: bool = True, tool_name: str = "Bash",
) -> str:
    """One assistant line dispatching a ``Bash`` (or, per #594, ``PowerShell``)
    tool call — the shape ``_launched_bash_dispatch_ids`` reads. This is the
    tool-call-id-keyed counterpart to ``_tool_result_line``'s
    ``toolUseResult``-keyed shape: #576 found real transcripts no longer
    carry ``backgroundTaskId`` on the result, so the launch has to be read
    off the ``tool_use`` block itself."""
    return json.dumps({
        "type": "assistant",
        "timestamp": _iso(ts),
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": tool_use_id,
                "name": tool_name,
                "input": {"command": "pwsh -File scripts/verify-before-ship.ps1",
                           "run_in_background": run_in_background},
            }],
        },
    })


def _tool_use_notification_line(ts: datetime, task_id: str, tool_use_id: str) -> str:
    """A completion notice carrying both correlation tags real transcripts
    use — ``<task-id>`` (the legacy scheme) and ``<tool-use-id>`` (#576).
    Paired with an immediate ``dequeue``, matching real delivery (#601) —
    see :func:`_task_notification_line`."""
    enqueue = json.dumps({
        "type": "queue-operation",
        "operation": "enqueue",
        "timestamp": _iso(ts),
        "content": (
            f"<task-notification>\n<task-id>{task_id}</task-id>\n"
            f"<tool-use-id>{tool_use_id}</tool-use-id>\n"
            "<status>completed</status>\n</task-notification>"
        ),
    })
    dequeue = json.dumps({"type": "queue-operation", "operation": "dequeue", "timestamp": _iso(ts)})
    return enqueue + "\n" + dequeue


def test_overlay_pending_bash_dispatch_without_background_task_id_keeps_working(tmp_path: Path):
    """A backgrounded Bash dispatch whose synchronous ack carries no
    ``backgroundTaskId`` (the real-world drift #576 found) must still be
    caught via the tool_use's own id, not just the legacy toolUseResult key."""
    stamp_time = NOW - timedelta(minutes=10)
    content = _bash_tool_use_line(stamp_time, "toolu_abc") + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "working"


def test_overlay_pending_powershell_dispatch_keeps_working(tmp_path: Path):
    """#594: this repo's own agents background long-running commands (e.g.
    the verify-before-ship gate) via the ``PowerShell`` tool, not ``Bash`` —
    the tool-call-id-keyed scheme must recognize it too, not just Bash."""
    stamp_time = NOW - timedelta(minutes=10)
    content = _bash_tool_use_line(stamp_time, "toolu_ps1", tool_name="PowerShell") + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "working"


def test_overlay_bash_dispatch_ack_alone_does_not_resolve_it(tmp_path: Path):
    """The synchronous 'launched in background' ack is an ordinary
    tool_result for the same tool_use_id — it must not be mistaken for
    completion, or the check would resolve the dispatch the instant it
    fires, defeating the whole point."""
    stamp_time = NOW - timedelta(minutes=10)
    content = (
        _bash_tool_use_line(stamp_time - timedelta(seconds=5), "toolu_abc") + "\n"
        + _tool_result_line(stamp_time, {"tool_use_id": "toolu_abc",
                                          "stdout": "Command running in background"}) + "\n"
    )
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "working"


def test_overlay_bash_dispatch_notified_by_tool_use_id_keeps_needs_you(tmp_path: Path):
    """Once the notification's ``<tool-use-id>`` tag resolves the dispatch,
    a genuine needs-you-family status still wins — mirrors the legacy
    scheme's own already-notified test, for the new id space. #608: the
    notification isn't a ``tool_result`` block, so the generic split's own
    scanner still sees the Bash call as unresolved — #813: that reads as
    ``tool-pending``, not a false idle-finished claim (a materially
    different, already-notified-and-genuinely-resolved concern, which is
    what keeps the dispatch itself out of ``working``/``stalled`` here)."""
    stamp_time = NOW - timedelta(minutes=10)
    content = (
        _bash_tool_use_line(stamp_time - timedelta(minutes=1), "toolu_abc") + "\n"
        + _tool_use_notification_line(
            stamp_time - timedelta(seconds=30), "btask1", "toolu_abc"
        ) + "\n"
    )
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "tool-pending"


def test_overlay_foreground_bash_tool_use_is_not_flagged_pending(tmp_path: Path):
    """A plain (non-backgrounded) Bash call must not trip the new check —
    only ``run_in_background: true`` marks a dispatch as pending. #608: the
    fixture never adds a resolving tool_result (out of scope for what this
    test targets), so the generic split's own scanner still sees it
    unresolved — #813: that reads as ``tool-pending`` rather than
    misreading an incomplete fixture as either a confirmed clean stop or a
    confirmed need for a human."""
    stamp_time = NOW - timedelta(minutes=10)
    content = _bash_tool_use_line(
        stamp_time, "toolu_abc", run_in_background=False
    ) + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "tool-pending"


# --------------------------------------- needs-you four-way split (#608)


def _tool_use_line(ts: datetime, tool_use_id: str, name: str) -> str:
    """One assistant line dispatching an arbitrary foreground tool call —
    the generic counterpart to :func:`_bash_tool_use_line`, for exercising
    :func:`src.board_transcript._refine_waiting_status`'s own tail scan
    rather than the background-dispatch-specific one."""
    return json.dumps({
        "type": "assistant",
        "timestamp": _iso(ts),
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_use_id, "name": name, "input": {}}],
        },
    })


def _generic_tool_result_line(ts: datetime, tool_use_id: str) -> str:
    """A plain, ordinary ``tool_result`` block for ``tool_use_id`` — the
    standard Anthropic content-block shape (no ``toolUseResult`` sibling),
    resolving whatever :func:`_tool_use_line` launched."""
    return json.dumps({
        "type": "user",
        "timestamp": _iso(ts),
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok"}],
        },
    })


def test_refine_pending_ask_user_question_is_awaiting_decision(tmp_path: Path):
    """A pending AskUserQuestion — Claude is blocked on a human picking an
    option, not just "needs a prompt"."""
    stamp_time = NOW - timedelta(minutes=10)
    content = _tool_use_line(stamp_time, "toolu_q1", "AskUserQuestion") + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "awaiting-decision"


def test_refine_pending_exit_plan_mode_is_awaiting_decision(tmp_path: Path):
    """A pending ExitPlanMode is the same kind of human-decision block as
    AskUserQuestion — approve or revise a plan."""
    stamp_time = NOW - timedelta(minutes=10)
    content = _tool_use_line(stamp_time, "toolu_p1", "ExitPlanMode") + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "awaiting-decision"


def test_refine_resolved_ask_user_question_is_idle_finished(tmp_path: Path):
    """Once the decision tool_use resolves (a real tool_result lands), the
    session isn't blocked on a decision anymore — nothing else is pending,
    so it reads as a clean stop."""
    stamp_time = NOW - timedelta(minutes=10)
    content = (
        _tool_use_line(stamp_time - timedelta(seconds=5), "toolu_q1", "AskUserQuestion") + "\n"
        + _generic_tool_result_line(stamp_time, "toolu_q1") + "\n"
    )
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "idle-finished"


def test_refine_pending_unrecognized_tool_use_is_tool_pending(tmp_path: Path):
    """A pending tool_use that isn't a decision tool and isn't a background
    dispatch (e.g. a permission-gated Read waiting on approval, or a plain
    Read still executing) is genuinely ambiguous between the two — #813
    reports it as its own ``tool-pending`` state rather than folding it into
    ``awaiting-input`` ("needs you"), which over-claimed a human was needed
    on every ordinary in-flight tool call."""
    stamp_time = NOW - timedelta(minutes=10)
    content = _tool_use_line(stamp_time, "toolu_r1", "Read") + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "tool-pending"


def test_refine_pending_bash_tool_use_is_tool_pending_not_needs_you(tmp_path: Path):
    """Regression (#813): the real observed timeline — a foreground Bash
    tool_use pending with no tool_result, and the needs-you stamp newer than
    the tool_use line's own timestamp (mirroring a long-running command
    during which the transcript writes nothing new until it returns). Before
    the fix this rendered ``awaiting-input`` ("needs you") mid-turn; it must
    now render ``tool-pending`` and route to Claude's turn, not Your turn."""
    tool_use_time = NOW - timedelta(minutes=12)
    content = _tool_use_line(tool_use_time, "toolu_b1", "Bash") + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, tool_use_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "tool-pending"
    columns = board.build_board(cards, {}, [])
    assert columns["your_turn"] == []
    assert [c["session_id"] for c in columns["claude_turn"]] == ["aaa"]


def test_refine_only_applies_to_needs_you_not_idle(tmp_path: Path):
    """The split is scoped to needs-you — a plain ``idle`` row with a pending
    decision tool_use must not be reclassified; idle isn't part of #608."""
    stamp_time = NOW - timedelta(minutes=10)
    content = _tool_use_line(stamp_time, "toolu_q1", "AskUserQuestion") + "\n"
    row = _state_row("E:/x/y", status="idle", updated_min_ago=10)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "idle"


# --------------------------------------------- stalled threshold (#608)


def test_stalled_background_dispatch_under_threshold_stays_working(tmp_path: Path):
    """A background dispatch outstanding 10 minutes is healthy waiting, not
    a stall — mirrors the fleet chief's own observed ~9-minute e2e-gate wait
    that must never misread as stalled."""
    stamp_time = NOW - timedelta(minutes=25)
    launch_time = NOW - timedelta(minutes=10)
    content = _tool_result_line(
        launch_time, {"stdout": "", "stderr": "", "backgroundTaskId": "btask1"}
    ) + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=25)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 30)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "working"


def test_stalled_background_dispatch_over_threshold_is_stalled(tmp_path: Path):
    """A background dispatch outstanding 35 minutes (past the 30-minute
    threshold) is a genuine anomaly worth surfacing distinctly from healthy
    waiting."""
    stamp_time = NOW - timedelta(minutes=40)
    launch_time = NOW - timedelta(minutes=35)
    content = _tool_result_line(
        launch_time, {"stdout": "", "stderr": "", "backgroundTaskId": "btask1"}
    ) + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=40)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 45)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "stalled"


def test_stalled_dispatch_with_unparseable_launch_stamp_stays_working(tmp_path: Path):
    """A dispatch with no parseable launch timestamp is genuinely
    outstanding but not age-able — per explicit direction, a low-confidence
    stalled call is worse than a late one, so this must not be guessed as
    stalled just because the sentinel age looks ancient."""
    stamp_time = NOW - timedelta(minutes=40)
    content = json.dumps({
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_x", "content": "ack"}],
        },
        "toolUseResult": {"stdout": "", "stderr": "", "backgroundTaskId": "btask1"},
        # No "timestamp" field at all — _parse_iso(None) is None.
    }) + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=40)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 45)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "working"


def test_stalled_status_stays_stalled_for_idle_hook_status(tmp_path: Path):
    """The stalled check only fires for a ``needs-you`` row — an ``idle``
    row with the same outstanding dispatch keeps the #464 pre-#608
    behavior (always ``working``), since idle is out of the four-way
    split's scope."""
    stamp_time = NOW - timedelta(minutes=40)
    launch_time = NOW - timedelta(minutes=35)
    content = _tool_result_line(
        launch_time, {"stdout": "", "stderr": "", "backgroundTaskId": "btask1"}
    ) + "\n"
    row = _state_row("E:/x/y", status="idle", updated_min_ago=40)
    row["transcript_path"] = _transcript_file(tmp_path, stamp_time, content)
    cards = board.merge_sessions([_live("aaa", "E:/x/y", 45)], {"t": row}, now=NOW)
    assert cards[0]["status"] == "working"


# ---------------------------------------------- build_board routing (#608)


def test_build_board_idle_finished_routes_to_claude_turn():
    """idle-finished isn't an alert — it belongs in Claude's turn alongside
    working/idle/unknown, not Your turn."""
    cards = [{"session_id": "s1", "status": "idle-finished", "label": ""}]
    columns = board.build_board(cards, {}, [])
    assert columns["your_turn"] == []
    assert [c["session_id"] for c in columns["claude_turn"]] == ["s1"]


def test_build_board_tool_pending_routes_to_claude_turn():
    """tool-pending (#813) isn't an alert either — genuinely ambiguous
    between "still executing" and "permission-gated", so it stays out of
    Your turn the same way idle-finished does."""
    cards = [{"session_id": "s1", "status": "tool-pending", "label": ""}]
    columns = board.build_board(cards, {}, [])
    assert columns["your_turn"] == []
    assert [c["session_id"] for c in columns["claude_turn"]] == ["s1"]


def test_build_board_needs_you_family_routes_to_your_turn():
    """stalled / awaiting-decision / awaiting-input all still mean "a human
    needs to look at this" and route to Your turn."""
    cards = [
        {"session_id": "s-stalled", "status": "stalled", "label": ""},
        {"session_id": "s-decision", "status": "awaiting-decision", "label": ""},
        {"session_id": "s-input", "status": "awaiting-input", "label": ""},
    ]
    columns = board.build_board(cards, {}, [])
    assert {c["session_id"] for c in columns["your_turn"]} == {
        "s-stalled", "s-decision", "s-input",
    }
    assert columns["claude_turn"] == []


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
    assert body["active_issues"]["available"] is False
    assert body["rate_limits"]["available"] is False
    assert body["columns"]["backlog"] == []
    assert body["generated_at"]


def test_api_board_marks_active_backlog_issue(
    webapp_client, monkeypatch
):
    client, app, _overrides = webapp_client
    fake = _FakeGh()
    monkeypatch.setattr(github_client.subprocess, "run", fake)
    github_client.refresh("ferraroroberto")

    active_file = Path(app.state.webapp_config.sessions_state_file).with_name(
        "active-issues.json"
    )
    active_file.write_text(json.dumps({
        "app-launcher#164": {
            "repo": "app-launcher", "number": 164,
            "branch": "feat/164-board", "started_at": _iso(datetime.now(timezone.utc)),
        },
    }), encoding="utf-8")

    body = client.get("/api/board").json()
    assert body["active_issues"]["available"] is True
    assert body["active_issues"]["count"] == 1
    assert body["columns"]["backlog"][0]["in_progress"] is True


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
    # #608: no transcript to check, so the needs-you split lands on the
    # safe generic value — still routed to Your turn either way.
    assert your_turn[0]["status"] == "awaiting-input"
    assert body["columns"]["claude_turn"] == []


def test_api_board_chief_never_lands_in_your_turn(webapp_client):
    """The standing fleet chief's needs-you card routes to Claude's turn, not
    Your turn (#575) — its Stop-hook status is its normal resting state
    between dispatches, not a transient alert like a worker session's."""
    client, app, overrides = webapp_client
    overrides["session"].list_sessions.return_value = [
        _live("chief-1", "E:/automation/fleet-config", 20, label="chief"),
    ]
    state_file = Path(app.state.webapp_config.sessions_state_file)
    state_file.write_text(json.dumps({
        "t-uuid": {"project": "fleet-config", "status": "needs-you",
                   "cwd": "E:/automation/fleet-config",
                   "updated_at": _iso(datetime.now(timezone.utc) - timedelta(minutes=3))},
    }), encoding="utf-8")

    body = client.get("/api/board").json()
    assert body["columns"]["your_turn"] == []
    claude_turn = body["columns"]["claude_turn"]
    assert [c["session_id"] for c in claude_turn] == ["chief-1"]
    # #608: no transcript to check, so the needs-you split lands on the
    # safe generic value — still routed to Claude's turn regardless (the
    # chief carve-out), and the client relabels it "standing by" for display.
    assert claude_turn[0]["status"] == "awaiting-input"


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
