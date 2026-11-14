"""The Board's "since you last looked" journal card: its reader and routes.

The chief appends events to ``chief-journal.jsonl`` beside its plan file; the
app reads it tolerantly and keeps one machine-wide "last look" stamp that the
chat send and the answer sheet move. These tests pin the tolerant reader, the
new-versus-old split, and the two routes.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.chief import journal as chief_journal


def _line(ts: str, kind: str = "merged", ref: str = "app-launcher#1", text: str = "t") -> str:
    return json.dumps({"ts": ts, "kind": kind, "ref": ref, "text": text})


def _write(path: Path, *lines: str) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_journal_sits_beside_the_plan_file(tmp_path):
    assert chief_journal.journal_path(tmp_path / "chief-plan.json") == tmp_path / "chief-journal.jsonl"


def test_missing_and_blank_files_are_empty(tmp_path):
    assert chief_journal.read_journal(tmp_path / "none.jsonl") == {"state": "empty"}
    assert chief_journal.read_journal(_write(tmp_path / "blank.jsonl", "", "  ")) == {"state": "empty"}


def test_unreadable_when_the_file_cannot_be_read_or_trusted(tmp_path):
    undecodable = tmp_path / "bad.jsonl"
    undecodable.write_bytes(b"\xff\xfe\x00")
    assert chief_journal.read_journal(undecodable) == {"state": "unreadable"}
    assert chief_journal.read_journal(tmp_path) == {"state": "unreadable"}  # a directory
    junk = _write(tmp_path / "junk.jsonl", "{not json", "[1]", _line("yesterday"))
    assert chief_journal.read_journal(junk) == {"state": "unreadable"}


def test_entries_are_newest_first_and_a_bad_line_is_skipped(tmp_path):
    path = _write(
        tmp_path / "j.jsonl",
        _line("2026-11-14T09:00:00Z", text="first"),
        "{broken",
        _line("2026-11-14T11:00:00Z", kind="dispatched", text="third"),
        _line("2026-11-14T10:00:00Z", kind="somethingnew", text="second"),
    )
    out = chief_journal.read_journal(path, "me", tmp_path / "no-look.json")
    assert out["state"] == "ok"
    assert [e["text"] for e in out["entries"]] == ["third", "second", "first"]
    # An unknown kind passes through for the card's neutral chip.
    assert out["entries"][1]["kind"] == "somethingnew"
    assert out["entries"][0]["ref_url"] == "https://github.com/me/app-launcher/issues/1"


def test_fields_are_cut_down_and_a_refless_entry_has_no_link(tmp_path):
    raw = json.dumps({
        "ts": "2026-11-14T09:00:00Z", "kind": "note", "ref": "", "text": "x" * 300,
        "extra": "ignored",
    })
    out = chief_journal.read_journal(_write(tmp_path / "j.jsonl", raw), "me")
    entry = out["entries"][0]
    assert set(entry) == {"ts", "kind", "ref", "text", "ref_url", "new"}
    assert len(entry["text"]) == chief_journal.MAX_TEXT
    assert entry["ref_url"] == ""


def test_new_entries_are_those_after_the_last_look(tmp_path):
    look = tmp_path / "look.json"
    look.write_text(json.dumps({"at": "2026-11-14T10:00:00Z"}), encoding="utf-8")
    path = _write(
        tmp_path / "j.jsonl",
        _line("2026-11-14T09:00:00Z", text="old"),
        _line("2026-11-14T10:00:00Z", text="at the look"),
        _line("2026-11-14T10:00:01Z", text="new"),
    )
    out = chief_journal.read_journal(path, "", look)
    assert [(e["text"], e["new"]) for e in out["entries"]] == [
        ("new", True), ("at the look", False), ("old", False),
    ]
    assert out["new_count"] == 1
    assert out["last_look"] == "2026-11-14T10:00:00Z"


def test_no_recorded_look_counts_everything_as_new(tmp_path):
    path = _write(tmp_path / "j.jsonl", _line("2026-11-14T09:00:00Z"))
    out = chief_journal.read_journal(path, "", tmp_path / "absent.json")
    assert out["new_count"] == 1 and out["last_look"] == ""


@pytest.mark.parametrize("content", ["{not json", "[]", '{"at": 5}', '{"at": "soon"}'])
def test_an_unreadable_look_reads_as_never(tmp_path, content):
    look = tmp_path / "look.json"
    look.write_text(content, encoding="utf-8")
    assert chief_journal.read_last_look(look) == ""


def test_record_look_stamps_utc_seconds(tmp_path):
    look = tmp_path / "sub" / "look.json"
    moment = datetime(2026, 11, 14, 12, 30, 5, 999999, tzinfo=timezone.utc)
    assert chief_journal.record_look(moment, look) == "2026-11-14T12:30:05Z"
    assert chief_journal.read_last_look(look) == "2026-11-14T12:30:05Z"


@pytest.fixture
def _bypass_gate(monkeypatch):
    from src import middleware
    monkeypatch.setattr(
        middleware.trust, "LOOPBACK_HOSTS",
        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
    )


def test_looked_route_is_passkey_gated(webapp_client):
    from src.middleware import _terminal_guard_level
    assert _terminal_guard_level("/api/board/chief/journal/looked") == "passkey"
    client, _, _ = webapp_client
    assert client.post("/api/board/chief/journal/looked").status_code == 403
    assert not chief_journal.CHIEF_JOURNAL_LOOK_FILE.exists()


def test_a_look_moves_the_new_entries_across_the_route(webapp_client, _bypass_gate):
    client, app, _ = webapp_client
    plan_file = Path(app.state.webapp_config.chief_plan_file)
    assert client.get("/api/board/chief-journal").json() == {"state": "empty"}

    _write(
        chief_journal.journal_path(plan_file),
        _line("2020-01-01T00:00:00Z", text="long ago"),
        _line("2999-01-01T00:00:00Z", text="in the future"),
    )
    before = client.get("/api/board/chief-journal").json()
    assert before["new_count"] == 2

    stamp = client.post("/api/board/chief/journal/looked").json()["last_look"]
    after = client.get("/api/board/chief-journal").json()
    assert after["last_look"] == stamp
    # Only the entry dated after the stamp is still new.
    assert [(e["text"], e["new"]) for e in after["entries"]] == [
        ("in the future", True), ("long ago", False),
    ]
    assert after["new_count"] == 1
