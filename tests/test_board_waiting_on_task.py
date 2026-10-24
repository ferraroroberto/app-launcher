"""A session waiting on its own background task is not waiting on a human.

Fixtures are the transcript tail a Claude lane leaves when its turn ends with a
backgrounded Bash plus a ``Monitor`` armed on it, in Claude Code's real line
shapes: a ``Monitor`` ack carries ``toolUseResult.taskId``, a monitor event is
a ``<task-notification>`` with no ``<status>``, and a monitor's end carries one.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from src import board
from tests.unit.board._support import (
    NOW, _bash_tool_use_line, _iso, _live, _msg_line, _state_row,
    _tool_use_notification_line, _transcript_file,
)

STAMP = NOW - timedelta(minutes=10)
FILLER = (json.dumps({"type": "system", "content": "x" * 400}) + "\n") * 700  # past the tail window


def _line(kind: str, ts: datetime, content: list, **extra) -> str:
    return json.dumps({
        "type": kind, "timestamp": _iso(ts), "message": {"role": kind, "content": content}, **extra,
    }) + "\n"


def _bash_launch(ts: datetime) -> str:
    """A backgrounded Bash and its synchronous "running" ack."""
    ack = [{"type": "tool_result", "tool_use_id": "toolu_gate", "content": "running in background"}]
    return _bash_tool_use_line(ts, "toolu_gate") + "\n" + _line("user", ts, ack)


def _monitor_launch(ts: datetime, task_id: str = "bmon1") -> str:
    """The assistant ``Monitor`` call and its launch ack."""
    call = [{"type": "tool_use", "id": "toolu_mon", "name": "Monitor", "input": {"command": "tail -f gate.log"}}]
    ack = [{"type": "tool_result", "tool_use_id": "toolu_mon", "content": "Monitor started"}]
    return _line("assistant", ts, call) + _line("user", ts, ack, toolUseResult={"taskId": task_id})


def _monitor_notification(ts: datetime, *, ended: bool, task_id: str = "bmon1") -> str:
    """A delivered (enqueue + dequeue) monitor event, or its terminal notice."""
    body = (
        f'<task-id>{task_id}</task-id>\n<status>completed</status>\n<summary>Monitor "gate" stream ended</summary>'
        if ended else
        f'<task-id>{task_id}</task-id>\n<summary>Monitor event: "gate"</summary>\n<event>[hb]</event>'
    )
    enqueue = {"type": "queue-operation", "operation": "enqueue", "timestamp": _iso(ts),
               "content": f"<task-notification>\n{body}\n</task-notification>"}
    dequeue = {"type": "queue-operation", "operation": "dequeue", "timestamp": _iso(ts)}
    return json.dumps(enqueue) + "\n" + json.dumps(dequeue) + "\n"


def _status(tmp_path: Path, content: str, *, hook: str = "needs-you", minutes: int = 10) -> str:
    row = _state_row("E:/x/y", status=hook, updated_min_ago=minutes)
    row["transcript_path"] = _transcript_file(tmp_path, NOW - timedelta(minutes=minutes), content)
    return board.merge_sessions([_live("aaa", "E:/x/y", 90)], {"t": row}, now=NOW)[0]["status"]


def test_background_bash_plus_monitor_is_waiting_on_task_and_events_do_not_end_it(tmp_path: Path):
    launched = _bash_launch(STAMP) + _monitor_launch(STAMP)
    assert _status(tmp_path, launched) == "waiting-on-task"
    assert _status(tmp_path, launched + _monitor_notification(STAMP, ended=False)) == "waiting-on-task"


def test_ended_monitor_and_notified_bash_return_to_the_normal_status(tmp_path: Path):
    content = (
        _bash_launch(STAMP) + _tool_use_notification_line(STAMP, "bgate", "toolu_gate") + "\n"
        + _monitor_launch(STAMP) + _monitor_notification(STAMP, ended=True)
        + _msg_line("assistant", STAMP) + "\n"
    )
    assert _status(tmp_path, content) == "idle-finished"


def test_monitor_whose_launch_scrolled_out_of_the_window_is_still_pending(tmp_path: Path):
    """Chatty events push the launch line past the tail window; the events
    themselves prove the monitor is alive until its end arrives."""
    events = _monitor_launch(STAMP) + FILLER + _monitor_notification(STAMP, ended=False)
    assert _status(tmp_path, events) == "waiting-on-task"
    ended = events + _monitor_notification(STAMP, ended=True) + _msg_line("assistant", STAMP) + "\n"
    assert _status(tmp_path, ended) == "idle-finished"


def test_monitor_pending_past_thirty_minutes_is_stalled(tmp_path: Path):
    assert _status(tmp_path, _monitor_launch(NOW - timedelta(minutes=35)), minutes=35) == "stalled"


def test_unreadable_tail_stays_awaiting_input(tmp_path: Path):
    assert _status(tmp_path, "not json at all\n") == "awaiting-input"


def test_waiting_on_task_is_claude_turn_and_not_counted_as_needs_you():
    card = {"session_id": "s1", "status": "waiting-on-task", "label": ""}
    columns = board.build_board([card], {}, [])
    assert columns["claude_turn"] == [card]
    assert columns["your_turn"] == []
