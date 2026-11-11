"""A chief plan queue row's status, declared once.

The chief writes each queue row's ``status`` through fleet-config's
``chief_ops.py plan`` helper (``docs/chief-plan.md`` there owns the format and
the transitions); this app only reads it. :class:`QueueStatus` is the closed
set that writer accepts, and :func:`queue_status` is the one rule mapping a
row to it. A reader asks it instead of re-spelling the strings.

The plan reader is tolerant by contract, so a value outside the set is not an
error: :func:`queue_status` answers ``None`` and the caller shows the raw word
as a neutral chip.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Mapping, Optional


class QueueStatus(StrEnum):
    """Every value the chief's writer accepts in a queue row's ``status``."""

    #: In the chief's order, not started.
    QUEUED = "queued"
    #: A lane is building it.
    BUILDING = "building"
    #: A lane is running its pre-ship gate.
    GATE = "gate"
    #: Shipped; the row stays until the chief prunes it.
    MERGED = "merged"
    #: Held back; the row's ``kind`` says why (``owner``, ``umbrella``, ``blocked-by``).
    PARKED = "parked"
    #: Waits on an open question to the owner.
    WAITING_ROBERTO = "waiting-roberto"


def queue_status(row: Optional[Mapping[str, Any]]) -> Optional[QueueStatus]:
    """The row's status; ``None`` for no row, no status or a value outside the set."""
    if not row:
        return None
    try:
        return QueueStatus(row.get("status"))
    except ValueError:
        return None
