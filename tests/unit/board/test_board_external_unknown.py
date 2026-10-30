"""Board backend: an external card has no process evidence, so it never claims alive or stalled."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from src import board

from tests.unit.board._support import (
    NOW,
    _state_row,
    _tool_result_line,
    _transcript_file,
)


def _external_with_old_dispatch(tmp_path: Path) -> list:
    """A needs-you row whose background dispatch has been outstanding 40 minutes,
    with a transcript touched a minute ago (so the row clears the freshness
    fallback and renders as an external card at all)."""
    launch_time = NOW - timedelta(minutes=40)
    content = _tool_result_line(
        launch_time, {"stdout": "", "stderr": "", "backgroundTaskId": "btask1"}
    ) + "\n"
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=35)
    row["transcript_path"] = _transcript_file(tmp_path, NOW - timedelta(minutes=1), content)
    return board.merge_sessions([], {"t": row}, now=NOW)


def _external_cards(tmp_path: Path, **extra) -> list:
    row = _state_row("E:/x/y", status="needs-you", updated_min_ago=2, **extra)
    row["transcript_path"] = _transcript_file(tmp_path, NOW - timedelta(minutes=1), "")
    return board.merge_sessions([], {"t": row}, now=NOW)


def test_external_card_carries_the_job_run_flag(tmp_path: Path):
    assert _external_cards(tmp_path, job_run=True)[0]["job_run"] is True


def test_external_card_without_the_flag_is_not_a_job_run(tmp_path: Path):
    assert _external_cards(tmp_path)[0]["job_run"] is False
    # Only a literal true counts; a stray truthy value is an ordinary session.
    assert _external_cards(tmp_path, job_run="yes")[0]["job_run"] is False


def test_external_card_alive_is_unknown_not_false(tmp_path: Path):
    cards = _external_with_old_dispatch(tmp_path)
    assert cards[0]["kind"] == "external"
    assert cards[0]["alive"] is None


def test_external_card_with_long_quiet_dispatch_is_not_stalled(tmp_path: Path):
    cards = _external_with_old_dispatch(tmp_path)
    assert cards[0]["status"] == "waiting-on-task"
    columns = board.build_board(cards, {}, [])
    assert columns["your_turn"] == []
    assert [c["kind"] for c in columns["claude_turn"]] == ["external"]
