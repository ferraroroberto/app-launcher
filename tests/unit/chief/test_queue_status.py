"""A chief plan queue row's status, declared in ``src/chief/queue_status.py``."""

from __future__ import annotations

import pytest

from src.board.backlog_buckets import _row_chips
from src.chief.queue_status import QueueStatus, queue_status


@pytest.mark.parametrize("value", list(QueueStatus))
def test_every_declared_status_reads_as_itself(value: QueueStatus) -> None:
    assert queue_status({"status": value.value}) is value


@pytest.mark.parametrize("row", [None, {}, {"status": ""}, {"status": "Queued"}, {"status": "done"}, {"status": 3}])
def test_a_value_outside_the_set_is_no_status(row) -> None:
    """A word the chief's writer never accepts is refused (None), never read as a status.

    state-machine-proof: chief-plan-queue-status
    """
    assert queue_status(row) is None


def test_an_unknown_status_still_shows_as_a_neutral_chip() -> None:
    assert _row_chips({"status": "someday"}) == ["someday"]
    assert _row_chips({"status": "queued"}) == []
    assert _row_chips({"status": "gate"}) == ["gate"]
    assert _row_chips({"status": "parked", "kind": "umbrella"}) == ["parked"]
