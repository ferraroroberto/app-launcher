"""Unit coverage for the durable chief-conversation pointer (issue #675).

The sidecar's whole job is to answer "which conversation is the chief" after
``sessions-state.json`` can no longer say — so these pin its validity rules
(expiry, missing transcript, corruption) and the degradation contract every
``board_state`` reader shares: unreadable is ``{}``, never an exception.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import chief_pointer


@pytest.fixture
def transcript(tmp_path: Path) -> Path:
    target = tmp_path / "conversation.jsonl"
    target.write_text("{}\n", encoding="utf-8")
    return target


@pytest.fixture
def pointer_file(tmp_path: Path) -> Path:
    return tmp_path / "chief-pointer.json"


def _write_raw(path: Path, **fields) -> None:
    path.write_text(json.dumps(fields), encoding="utf-8")


class TestReadChiefPointer:
    def test_round_trip(self, pointer_file, transcript):
        chief_pointer.write_chief_pointer(
            "conv-uuid", transcript, "live", path=pointer_file
        )
        pointer = chief_pointer.read_chief_pointer(pointer_file)
        assert pointer["session_id"] == "conv-uuid"
        assert pointer["transcript_path"] == str(transcript)
        assert pointer["source"] == "live"
        assert pointer["seen_at"].endswith("Z")

    def test_missing_file_reads_empty(self, pointer_file):
        assert chief_pointer.read_chief_pointer(pointer_file) == {}

    def test_corrupt_json_reads_empty(self, pointer_file):
        pointer_file.write_text("{not json", encoding="utf-8")
        assert chief_pointer.read_chief_pointer(pointer_file) == {}

    def test_non_dict_payload_reads_empty(self, pointer_file):
        pointer_file.write_text("[1, 2, 3]", encoding="utf-8")
        assert chief_pointer.read_chief_pointer(pointer_file) == {}

    def test_missing_session_id_reads_empty(self, pointer_file, transcript):
        _write_raw(
            pointer_file,
            transcript_path=str(transcript),
            seen_at="2026-07-29T09:00:00Z",
        )
        assert chief_pointer.read_chief_pointer(pointer_file) == {}

    def test_unparseable_seen_at_reads_empty(self, pointer_file, transcript):
        _write_raw(
            pointer_file,
            session_id="conv-uuid",
            transcript_path=str(transcript),
            seen_at="whenever",
        )
        assert chief_pointer.read_chief_pointer(pointer_file) == {}

    def test_expired_pointer_reads_empty(self, pointer_file, transcript):
        """Roberto's call (#675): 7 days. A chief he moved on from a fortnight
        ago must not come back just because nothing fresher exists."""
        stale = datetime.now(timezone.utc) - timedelta(days=8)
        _write_raw(
            pointer_file,
            session_id="conv-uuid",
            transcript_path=str(transcript),
            seen_at=stale.isoformat().replace("+00:00", "Z"),
        )
        assert chief_pointer.read_chief_pointer(pointer_file) == {}

    def test_just_inside_the_window_still_reads(self, pointer_file, transcript):
        recent = datetime.now(timezone.utc) - timedelta(days=6, hours=23)
        _write_raw(
            pointer_file,
            session_id="conv-uuid",
            transcript_path=str(transcript),
            seen_at=recent.isoformat().replace("+00:00", "Z"),
        )
        assert (
            chief_pointer.read_chief_pointer(pointer_file)["session_id"]
            == "conv-uuid"
        )

    def test_deleted_transcript_reads_empty(self, pointer_file, transcript):
        """Validity, not the caller's problem: ``claude --resume`` would fail
        on a conversation whose transcript is gone."""
        chief_pointer.write_chief_pointer(
            "conv-uuid", transcript, "live", path=pointer_file
        )
        transcript.unlink()
        assert chief_pointer.read_chief_pointer(pointer_file) == {}

    def test_now_is_injectable(self, pointer_file, transcript):
        chief_pointer.write_chief_pointer(
            "conv-uuid", transcript, "live", path=pointer_file
        )
        future = datetime.now(timezone.utc) + timedelta(days=8)
        assert chief_pointer.read_chief_pointer(pointer_file, now=future) == {}

    def test_module_default_path_is_used_when_none_given(
        self, pointer_file, transcript, monkeypatch
    ):
        monkeypatch.setattr(chief_pointer, "CHIEF_POINTER_FILE", pointer_file)
        chief_pointer.write_chief_pointer("conv-uuid", transcript, "live")
        assert chief_pointer.read_chief_pointer()["session_id"] == "conv-uuid"


class TestWriteChiefPointer:
    def test_write_is_atomic_and_leaves_no_temp_file(
        self, pointer_file, transcript
    ):
        chief_pointer.write_chief_pointer(
            "conv-uuid", transcript, "live", path=pointer_file
        )
        siblings = {p.name for p in pointer_file.parent.iterdir()}
        assert pointer_file.name in siblings
        assert not [name for name in siblings if name.endswith(".tmp")]

    def test_write_creates_the_parent_directory(self, tmp_path, transcript):
        target = tmp_path / "nested" / "chief-pointer.json"
        chief_pointer.write_chief_pointer(
            "conv-uuid", transcript, "live", path=target
        )
        assert json.loads(target.read_text(encoding="utf-8"))["session_id"] == (
            "conv-uuid"
        )

    def test_write_overwrites_a_previous_pointer(self, pointer_file, transcript):
        chief_pointer.write_chief_pointer(
            "old-uuid", transcript, "live", path=pointer_file
        )
        chief_pointer.write_chief_pointer(
            "new-uuid", transcript, "ensure-resume", path=pointer_file
        )
        pointer = chief_pointer.read_chief_pointer(pointer_file)
        assert pointer["session_id"] == "new-uuid"
        assert pointer["source"] == "ensure-resume"

    def test_unwritable_path_never_raises(self, tmp_path, transcript):
        """Fire-and-forget off a poll path: a failed write must cost nothing
        more than the next Resume falling back to the row scan."""
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        chief_pointer.write_chief_pointer(
            "conv-uuid", transcript, "live", path=blocker / "chief-pointer.json"
        )
