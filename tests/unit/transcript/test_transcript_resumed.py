"""Coding-tab transcript: a resumed Copilot session keeps its whole history."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from src.session_view import transcript_locate

_STARTED = 1_789_682_250.0
_CWD = r"E:\work\project"
# A pid no process holds.
_DEAD_PID = 99_999_999


def _iso(offset_s: float) -> str:
    return datetime.fromtimestamp(_STARTED + offset_s, timezone.utc).isoformat()


def _line(kind: str, offset_s: float) -> str:
    return json.dumps({"type": kind, "timestamp": _iso(offset_s), "data": {}}) + "\n"


def _folder(state: Path, name: str, created_s: float, text: str) -> Path:
    folder = state / name
    folder.mkdir(parents=True)
    (folder / "workspace.yaml").write_text(
        f"id: {name}\ncwd: {_CWD}\ncreated_at: {_iso(created_s)}\n", encoding="utf-8"
    )
    events = folder / "events.jsonl"
    events.write_text(text, encoding="utf-8")
    return events


def _history(state: Path, name: str = "a-history") -> Path:
    """A conversation from an earlier run, not yet written to by this one."""
    events = _folder(state, name, -86_400, _line("user.message", -86_390))
    os.utime(events, (_STARTED - 86_000, _STARTED - 86_000))
    return events


def _launch(state: Path, text: Optional[str] = None) -> Path:
    """The folder every interactive launch creates for itself."""
    return _folder(state, "b-launch", 4, _line("session.start", 4) if text is None else text)


def _lock(events: Path, opened_s: float, pid: Optional[int] = None) -> None:
    """The ``inuse.<pid>.lock`` Copilot keeps in every folder a process opened."""
    owner = os.getpid() if pid is None else pid
    lock = events.parent / f"inuse.{owner}.lock"
    lock.write_text(f"{owner}\n", encoding="utf-8")
    os.utime(lock, (_STARTED + opened_s, _STARTED + opened_s))


def _find(state: Path, monkeypatch: pytest.MonkeyPatch) -> Optional[Path]:
    monkeypatch.setattr(transcript_locate, "_COPILOT_STATE_DIR", state)
    session: Dict[str, Any] = {
        "session_id": "s1", "agent": "copilot", "project_dir": _CWD, "started_at": _STARTED,
    }
    return transcript_locate.find_copilot_transcript(session)


def test_a_conversation_picked_after_launch_replaces_the_empty_launch_folder(
    tmp_path: Path, monkeypatch,
):
    """``--resume`` (and ``/resume``) leaves the launch's own folder holding no
    turn and appends to the picked one. The process's locks name both; the
    later one is on screen."""
    history, launch = _history(tmp_path), _launch(tmp_path)
    _lock(launch, 4)
    _lock(history, 900)
    assert _find(tmp_path, monkeypatch) == history


def test_a_resume_by_id_is_found_before_its_first_prompt(tmp_path: Path, monkeypatch):
    history = _history(tmp_path)
    _lock(history, 4)
    assert _find(tmp_path, monkeypatch) == history


def test_a_resume_by_id_is_found_by_the_resume_its_log_records(tmp_path: Path, monkeypatch):
    history = _history(tmp_path)
    with history.open("a", encoding="utf-8") as fh:
        fh.write(_line("session.resume", 4))
    os.utime(history, (_STARTED + 4, _STARTED + 4))
    assert _find(tmp_path, monkeypatch) == history


def test_two_folders_reopened_in_one_window_refuse(tmp_path: Path, monkeypatch):
    _lock(_history(tmp_path), 4)
    _lock(_history(tmp_path, "c-other"), 5)
    assert _find(tmp_path, monkeypatch) is None


def test_a_session_with_its_own_turns_is_never_swapped(tmp_path: Path, monkeypatch):
    history = _history(tmp_path)
    launch = _launch(tmp_path, _line("user.message", 9))
    _lock(launch, 4)
    _lock(history, 900)
    assert _find(tmp_path, monkeypatch) == launch


def test_only_this_launch_s_own_process_is_followed(tmp_path: Path, monkeypatch):
    """Another process's lock, and one a dead process left behind, name nothing."""
    history, launch = _history(tmp_path), _launch(tmp_path)
    _lock(launch, 4)
    _lock(history, 900, pid=os.getppid())
    _lock(_history(tmp_path, "c-crashed"), 900, pid=_DEAD_PID)
    assert _find(tmp_path, monkeypatch) == launch


def test_the_conversation_opened_last_wins(tmp_path: Path, monkeypatch):
    """``/new`` after a resume: the resumed one is no longer on screen."""
    history, launch = _history(tmp_path), _launch(tmp_path)
    fresh = _folder(tmp_path, "d-new", 1200, _line("session.start", 1200))
    _lock(launch, 4)
    _lock(history, 900)
    _lock(fresh, 1200)
    assert _find(tmp_path, monkeypatch) == fresh
