"""Which user turns are automatic: the injection ledger, the fixed doorbell line, compact summaries."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from src import audit, board
from src.session_view import automatic_turns as at
from src.transcript_flavors.claude import claude_entries
from tests.unit.transcript._support import (
    _assistant,
    _lines_of,
    _live,
    _user,
    _write_jsonl,
)

SID = "chief-sid"
#: 2026-11-03T12:00:00Z
T0 = 1_793_707_200.0


def _entry(text: str, when: float, **extra: Any) -> Dict[str, Any]:
    from datetime import datetime, timezone

    stamp = datetime.fromtimestamp(when, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {"kind": "user", "text": text, "timestamp": stamp, **extra}


# ------------------------------------------------------------ the ledger


class TestLedger:
    def test_a_recorded_row_reads_back_oldest_first(self, tmp_path: Path):
        assert at.record_injected(SID, "later", "doorbell", directory=tmp_path, now=T0 + 5)
        assert at.record_injected(SID, "earlier", "doorbell", directory=tmp_path, now=T0)
        rows = at.read_ledger(SID, tmp_path)
        assert [r[0] for r in rows] == [T0, T0 + 5]
        assert rows[0][2] == "doorbell"

    def test_a_session_with_no_file_has_an_empty_ledger(self, tmp_path: Path):
        assert at.read_ledger(SID, tmp_path) == []

    def test_an_unreadable_ledger_is_unknown_not_empty(self, tmp_path: Path):
        path = at.ledger_path(SID, tmp_path)
        path.write_bytes(b"\xff\xfe not utf-8 \xff")
        assert at.read_ledger(SID, tmp_path) is None

    def test_a_torn_line_is_skipped_and_the_rest_still_read(self, tmp_path: Path):
        at.record_injected(SID, "a", "doorbell", directory=tmp_path, now=T0)
        with at.ledger_path(SID, tmp_path).open("a", encoding="utf-8") as fh:
            fh.write('{"ts": 1, "sha25\n')
        at.record_injected(SID, "b", "doorbell", directory=tmp_path, now=T0 + 1)
        assert len(at.read_ledger(SID, tmp_path)) == 2

    @pytest.mark.parametrize("bad", ["", "../escape", "a/b", "a\\b", "x.y"])
    def test_an_id_that_is_not_a_plain_token_never_names_a_path(self, tmp_path: Path, bad: str):
        assert at.ledger_path(bad, tmp_path) is None
        assert at.record_injected(bad, "a", "doorbell", directory=tmp_path) is False
        assert at.read_ledger(bad, tmp_path) is None
        assert list(tmp_path.iterdir()) == []

    def test_a_write_that_fails_reports_false_and_raises_nothing(self, tmp_path: Path):
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        assert at.record_injected(SID, "a", "doorbell", directory=blocker) is False


# ----------------------------------------------------- mark_automatic


class TestMarkAutomatic:
    def test_the_fixed_doorbell_line_is_automatic_without_any_ledger(self, tmp_path: Path):
        entries = [_entry(at.DOORBELL_LINE, T0)]
        assert at.mark_automatic(SID, entries, directory=tmp_path) == 1
        assert entries[0]["auto"] == "doorbell"

    def test_a_ledger_row_marks_the_matching_turn(self, tmp_path: Path):
        at.record_injected(SID, "tick the lane", "lane-steer", directory=tmp_path, now=T0)
        entries = [_entry("tick the lane", T0 + 2)]
        at.mark_automatic(SID, entries, directory=tmp_path)
        assert entries[0]["auto"] == "lane-steer"

    def test_the_same_text_with_no_ledger_row_stays_visible(self, tmp_path: Path):
        entries = [_entry("tick the lane", T0)]
        assert at.mark_automatic(SID, entries, directory=tmp_path) == 0
        assert "auto" not in entries[0]

    def test_one_row_is_spent_by_one_turn(self, tmp_path: Path):
        at.record_injected(SID, "again", "lane-steer", directory=tmp_path, now=T0)
        entries = [_entry("again", T0 + 1), _entry("again", T0 + 2)]
        at.mark_automatic(SID, entries, directory=tmp_path)
        assert ["auto" in e for e in entries] == [True, False]

    def test_rows_spend_across_the_batches_of_one_response(self, tmp_path: Path):
        at.record_injected(SID, "again", "lane-steer", directory=tmp_path, now=T0)
        settled, pending = [_entry("again", T0 + 1)], [_entry("again", T0 + 2)]
        at.mark_automatic(SID, settled, pending, directory=tmp_path)
        assert ["auto" in settled[0], "auto" in pending[0]] == [True, False]

    def test_a_line_written_before_the_row_is_not_that_rows(self, tmp_path: Path):
        at.record_injected(SID, "later row", "lane-steer", directory=tmp_path, now=T0 + 60)
        entries = [_entry("later row", T0)]
        at.mark_automatic(SID, entries, directory=tmp_path)
        assert "auto" not in entries[0]

    def test_a_line_long_after_the_row_is_not_that_rows(self, tmp_path: Path):
        at.record_injected(SID, "old row", "lane-steer", directory=tmp_path, now=T0)
        entries = [_entry("old row", T0 + at.LEDGER_LAG_SECONDS + 1)]
        at.mark_automatic(SID, entries, directory=tmp_path)
        assert "auto" not in entries[0]

    def test_an_unreadable_ledger_marks_nothing_from_it(self, tmp_path: Path):
        at.ledger_path(SID, tmp_path).write_bytes(b"\xff\xfe")
        entries = [_entry("tick the lane", T0)]
        assert at.mark_automatic(SID, entries, directory=tmp_path) == 0
        assert "auto" not in entries[0]

    def test_a_board_answers_message_is_never_automatic(self, tmp_path: Path):
        text = "Answers from the Board (1 of 1):\n1. Ship it"
        entries = [_entry(text, T0)]
        at.mark_automatic(SID, entries, directory=tmp_path)
        assert "auto" not in entries[0]

    def test_a_typed_message_is_never_automatic(self, tmp_path: Path):
        entries = [_entry("what did the lanes find?", T0)]
        at.mark_automatic(SID, entries, directory=tmp_path)
        assert "auto" not in entries[0]

    def test_only_a_whole_doorbell_line_matches(self, tmp_path: Path):
        entries = [_entry(at.DOORBELL_LINE + " please", T0), _entry(" " + at.DOORBELL_LINE[1:], T0)]
        at.mark_automatic(SID, entries, directory=tmp_path)
        assert all("auto" not in e for e in entries)

    def test_other_kinds_sidechains_and_capped_turns_are_left_alone(self, tmp_path: Path):
        entries = [
            {"kind": "assistant", "text": at.DOORBELL_LINE, "timestamp": "2026-11-03T12:00:00Z"},
            _entry(at.DOORBELL_LINE, T0, sidechain=True),
            _entry(at.DOORBELL_LINE, T0, truncated=True),
        ]
        assert at.mark_automatic(SID, entries, directory=tmp_path) == 0

    def test_an_entry_already_marked_keeps_its_origin(self, tmp_path: Path):
        entries = [_entry(at.DOORBELL_LINE, T0, auto="compact-summary")]
        at.mark_automatic(SID, entries, directory=tmp_path)
        assert entries[0]["auto"] == "compact-summary"


# ----------------------------------------------- the claude line grammar


def _built(rows: List[Dict[str, Any]], tmp_path: Path) -> List[Dict[str, Any]]:
    return claude_entries(_lines_of(_write_jsonl(tmp_path / "t.jsonl", rows)))


def test_a_compact_summary_is_automatic(tmp_path: Path):
    entries = _built([
        _user("This session is being continued from a previous conversation…",
              isCompactSummary=True, isVisibleInTranscriptOnly=True),
        _assistant([{"type": "text", "text": "Resuming."}], "m1"),
    ], tmp_path)
    assert [e.get("auto") for e in entries if e["kind"] == "user"] == ["compact-summary"]


def test_a_typed_prompt_carries_no_origin(tmp_path: Path):
    entries = _built([_user("Please fix the bug")], tmp_path)
    assert "auto" not in entries[0]


@pytest.mark.parametrize("flag", [False, None, "true", 1])
def test_only_a_real_true_flag_marks_a_summary(tmp_path: Path, flag: Any):
    entries = _built([_user("a prompt", isCompactSummary=flag)], tmp_path)
    assert "auto" not in entries[0]


# ------------------------------------------------------ the endpoint


class TestEndpoint:
    def _serve(self, webapp_client, monkeypatch, tmp_path: Path, rows: List[Dict[str, Any]]):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(kind="remote")]
        path = _write_jsonl(tmp_path / "t.jsonl", rows)
        monkeypatch.setattr(
            board, "state_row_for_session", lambda live, rows, sid: {"transcript_path": str(path)}
        )
        monkeypatch.setattr(audit, "_SESSIONS_DIR", tmp_path / "sessions")
        return client

    def test_a_page_marks_the_doorbell_turn_and_only_it(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path: Path
    ):
        client = self._serve(webapp_client, monkeypatch, tmp_path, [
            _user("a real question"),
            _assistant([{"type": "text", "text": "An answer."}], "m1"),
            _user(at.DOORBELL_LINE),
            _assistant([{"type": "text", "text": "Nothing new."}], "m2"),
        ])
        body = client.get("/api/claude-code/sessions/s1/transcript").json()
        users = [(e["text"], e.get("auto")) for e in body["entries"] if e["kind"] == "user"]
        assert users == [("a real question", None), (at.DOORBELL_LINE, "doorbell")]

    def test_a_page_marks_a_turn_from_the_ledger(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path: Path
    ):
        client = self._serve(webapp_client, monkeypatch, tmp_path, [
            _user("steer the lane"),
            _assistant([{"type": "text", "text": "Done."}], "m1"),
        ])
        at.record_injected("s1", "steer the lane", "lane-steer", directory=audit.sessions_dir(), now=0)
        # The fixture timestamp is 2026-09-14; a row stamped in 1970 is outside the lag window.
        body = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert all("auto" not in e for e in body["entries"])
        (audit.sessions_dir() / "s1.injected.log").unlink()
        at.record_injected(
            "s1", "steer the lane", "lane-steer", directory=audit.sessions_dir(),
            now=1_789_380_000.0,  # 2026-09-14T10:00:00Z, the fixture's stamp
        )
        body = client.get("/api/claude-code/sessions/s1/transcript?limit=41").json()
        marked = [e.get("auto") for e in body["entries"] if e["kind"] == "user"]
        assert marked == ["lane-steer"]
