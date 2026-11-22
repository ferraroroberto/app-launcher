"""Session-state reconcile: dead rows become ``ended`` and leave the hot set; live and unverifiable ones never do.

Fake session lists and fake PIDs only: nothing here touches a real session or process.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src import board
from src.board import state_reconcile
from src.board.state import STATUS_ENDED
from src.clients import session as session_client
from tests.unit.board._support import NOW, _live, _state_row


def _row(sid: str, *, updated_min_ago: int = 30, **extra) -> Dict[str, Any]:
    return _state_row(
        "E:/x/y", status="needs-you", updated_min_ago=updated_min_ago,
        launcher_session_id=sid, agent="copilot", **extra,
    )


def _write(path: Path, rows: Dict[str, Any]) -> Path:
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path


def _reconcile(path: Path, live: List[Dict[str, Any]], pids=frozenset()):
    return state_reconcile.reconcile_rows(
        path, lambda: live, now=NOW, pid_alive=lambda pid: pid in pids,
    )


def _on_disk(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_dead_rows_end_and_leave_the_board_and_session_builds(tmp_path: Path):
    path = _write(tmp_path / "state.json", {
        "alive-row": _row("sid-live"),
        "dead-row": _row("sid-dead"),
    })
    live = [_live("sid-live", "E:/x/y", 20, agent="copilot")]

    result = _reconcile(path, live)

    assert (result.ended, result.live, result.unknown) == (1, 1, 0)
    assert _on_disk(path)["dead-row"]["status"] == STATUS_ENDED
    assert _on_disk(path)["alive-row"]["status"] == "needs-you"
    state = board.read_sessions_state(path, now=NOW)
    assert set(state["rows"]) == {"alive-row"}
    assert set(state["ended_rows"]) == {"dead-row"}
    # Only the live session builds a card, and no dead row is walked at all.
    cards = board.merge_sessions(live, state["rows"], now=NOW)
    assert [c["session_id"] for c in cards] == ["sid-live"]
    assert board.attach_shared_names(live, state["rows"])[0]["session_id"] == "sid-live"


def test_a_row_that_cannot_be_checked_stays_unknown_and_unchanged(tmp_path: Path):
    legacy = _state_row("E:/x/y", status="needs-you", updated_min_ago=30)
    path = _write(tmp_path / "state.json", {"legacy": legacy})

    result = _reconcile(path, [])

    assert (result.ended, result.unknown) == (0, 1)
    assert _on_disk(path)["legacy"] == legacy


def test_a_live_session_is_never_ended(tmp_path: Path):
    rows = {f"row-{n}": _row(f"sid-{n}", updated_min_ago=600) for n in range(5)}
    path = _write(tmp_path / "state.json", rows)
    live = [_live(f"sid-{n}", "E:/x/y", 700, agent="copilot") for n in range(5)]

    result = _reconcile(path, live)

    assert (result.ended, result.live) == (0, 5)
    assert _on_disk(path) == rows


def test_a_listed_but_not_alive_session_does_not_keep_its_row(tmp_path: Path):
    path = _write(tmp_path / "state.json", {"r": _row("sid-gone")})
    live = [_live("sid-gone", "E:/x/y", 20, agent="copilot", alive=False)]

    assert _reconcile(path, live).ended == 1


@pytest.mark.parametrize("session_host_down", [True, False])
def test_an_unreadable_source_changes_nothing(tmp_path: Path, session_host_down: bool):
    rows = {"r": _row("sid-dead")}
    path = tmp_path / "state.json"
    if session_host_down:
        _write(path, rows)

        def boom():
            raise session_client.SessionHostError("down")

        result = state_reconcile.reconcile_rows(path, boom, now=NOW)
    else:
        path.write_text("{not json", encoding="utf-8")
        result = _reconcile(path, [])
    assert result.skipped and result.ended == 0
    if session_host_down:
        assert _on_disk(path) == rows


def test_a_row_touched_in_the_last_two_minutes_is_left_for_the_next_pass(tmp_path: Path):
    path = _write(tmp_path / "state.json", {"new": _row("sid-new", updated_min_ago=1)})

    assert _reconcile(path, []).ended == 0
    assert _on_disk(path)["new"]["status"] == "needs-you"


def test_a_recorded_pid_decides_a_row_the_host_does_not_list():
    alive = {"launcher_session_id": "", "pid": 4242}
    gone = {"launcher_session_id": "", "pid": 4243}
    both = {"launcher_session_id": "sid-x", "pid": 4242}

    def classify(row):
        return state_reconcile.classify_row(row, set(), pid_alive=lambda pid: pid == 4242)

    assert classify(alive) == state_reconcile.VERDICT_LIVE
    assert classify(gone) == state_reconcile.VERDICT_ENDED
    # A running process outranks the host's silence about it.
    assert classify(both) == state_reconcile.VERDICT_LIVE


def test_a_pid_probe_that_fails_is_unknown_not_ended():
    def boom(pid: int) -> bool:
        raise OSError("probe failed")

    row = {"launcher_session_id": "sid-x", "pid": 7}
    assert state_reconcile.classify_row(row, set(), pid_alive=boom) == state_reconcile.VERDICT_UNKNOWN


def test_a_row_a_hook_rewrote_during_the_pass_is_kept(tmp_path: Path, monkeypatch):
    path = _write(tmp_path / "state.json", {"r": _row("sid-gone")})

    def hook_speaks_up():
        rows = _on_disk(path)
        rows["r"]["updated_at"] = (NOW - timedelta(seconds=5)).isoformat()
        rows["r"]["status"] = "working"
        _write(path, rows)
        return []

    result = state_reconcile.reconcile_rows(path, hook_speaks_up, now=NOW, pid_alive=lambda pid: False)

    assert result.ended == 0
    assert _on_disk(path)["r"]["status"] == "working"


def test_the_pass_is_idempotent_and_keeps_ended_history(tmp_path: Path):
    path = _write(tmp_path / "state.json", {"r": _row("sid-gone")})

    assert _reconcile(path, []).ended == 1
    again = _reconcile(path, [])

    assert (again.ended, again.live, again.unknown) == (0, 0, 0)
    assert "ended_at" in _on_disk(path)["r"]


def test_a_resumed_session_is_back_in_the_hot_set_once_its_hook_writes(tmp_path: Path):
    path = _write(tmp_path / "state.json", {"r": _row("sid-gone")})
    _reconcile(path, [])
    rows = _on_disk(path)
    rows["r"] = _row("sid-gone", updated_min_ago=0)  # what the hook's next write replaces it with
    _write(path, rows)

    assert set(board.read_sessions_state(path, now=NOW)["rows"]) == {"r"}
