"""Claude's turn marks the sessions that have nothing left to do as ``finished``.

The column keeps every card; the Board draws the finished ones in a folded
group outside the lane's count, so only the unambiguous cases may carry the flag.
"""

from __future__ import annotations

import pytest

from src import board
from src.board import IDLE_FINISHED_AFTER_SECONDS


def _card(status: str, age=None, **extra):
    card = {"session_id": "s1", "status": status, "label": "", "age_seconds": age}
    card.update(extra)
    return card


def _flag(card) -> bool:
    columns = board.build_board([card], {}, [])
    assert columns["your_turn"] == []
    (placed,) = columns["claude_turn"]
    return placed["finished"]


def test_idle_finished_is_finished_at_any_age():
    assert _flag(_card("idle-finished", age=5)) is True
    assert _flag(_card("idle-finished", age=None)) is True


def test_idle_past_the_threshold_is_finished_and_a_fresh_one_is_not():
    assert _flag(_card("idle", age=IDLE_FINISHED_AFTER_SECONDS)) is True
    assert _flag(_card("idle", age=IDLE_FINISHED_AFTER_SECONDS - 1)) is False


def test_idle_with_no_age_anchor_stays_active():
    assert _flag(_card("idle", age=None)) is False


@pytest.mark.parametrize("status", ["working", "unknown", "tool-pending", "waiting-on-task"])
def test_every_other_lane_status_stays_active_however_old(status):
    assert _flag(_card(status, age=IDLE_FINISHED_AFTER_SECONDS * 10)) is False


def test_the_chief_is_never_finished():
    assert _flag(_card("idle-finished", age=99999, label="chief")) is False


def test_the_flag_does_not_move_a_card_between_columns():
    cards = [
        {"session_id": "a", "status": "idle-finished", "label": "", "age_seconds": 9},
        {"session_id": "b", "status": "working", "label": "", "age_seconds": 9},
        {"session_id": "c", "status": "awaiting-input", "label": "", "age_seconds": 9},
    ]
    columns = board.build_board(cards, {}, [])
    assert [c["session_id"] for c in columns["claude_turn"]] == ["a", "b"]
    assert [c["session_id"] for c in columns["your_turn"]] == ["c"]
    assert "finished" not in columns["your_turn"][0]
