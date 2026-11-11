"""A job run's lifecycle status, declared once.

``run.json``'s ``status`` field is the run's state. It is written by several
processes (the webapp's admission and Kill routes, the detached executor, the
stranded-run reaper, the mutex-queue drain) and read by every Jobs surface, so
the closed set of values and the groupings readers need live here. A reader
asks :func:`run_status` or :func:`is_active`; it never compares the raw string.

The lifecycle (``docs/job-run-state.md`` has the full table):

* ``queued`` -> ``pending`` (the mutex drain spawns the head) or ``failed``
* ``pending`` -> ``running`` (the executor started the child) or ``failed``
* ``running`` -> ``success`` or ``failed``
* ``success`` and ``failed`` are terminal: no writer moves a run out of them.
* ``skipped``, ``dry_run_success`` and ``dry_run_failed`` are written once, already
  terminal, by fires that spawn nothing (the cooldown gate, the dry-run check).

The terminal rule is enforced where two writers race: the reaper's guarded
write re-checks :func:`is_active` under the run lock and refuses to overwrite a
run the executor already finished.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Mapping, Optional


class RunStatus(StrEnum):
    """Every value ``run.json``'s ``status`` may hold."""

    #: Waiting in its mutex group's FIFO behind a running holder.
    QUEUED = "queued"
    #: Admitted and spawned; the executor has not started the child yet.
    PENDING = "pending"
    #: The executor's child is running.
    RUNNING = "running"
    #: Finished with a zero exit code.
    SUCCESS = "success"
    #: Finished any other way: non-zero exit, killed, reaped, refused, spawn error.
    FAILED = "failed"
    #: A fire the executor's cooldown gate declined; nothing was spawned.
    SKIPPED = "skipped"
    #: A dry-run check resolved the invocation; nothing was spawned.
    DRY_RUN_SUCCESS = "dry_run_success"
    #: A dry-run check could not resolve the invocation; the reason is in ``note``.
    DRY_RUN_FAILED = "dry_run_failed"


#: Not finished yet: holds its mutex group and may still be killed or reaped.
ACTIVE = frozenset({RunStatus.PENDING, RunStatus.RUNNING})
#: Terminal: the run has an outcome and no writer moves it again.
FINISHED = frozenset({RunStatus.SUCCESS, RunStatus.FAILED})
#: Written once and terminal, but no child ran: never a cooldown anchor or a stat.
NOT_A_FIRE = frozenset({RunStatus.SKIPPED, RunStatus.DRY_RUN_SUCCESS, RunStatus.DRY_RUN_FAILED})


def run_status(record: Optional[Mapping[str, Any]]) -> Optional[RunStatus]:
    """The state a run record is in; ``None`` for no record or a value outside the set."""
    if not record:
        return None
    try:
        return RunStatus(record.get("status"))
    except ValueError:
        return None


def is_active(record: Optional[Mapping[str, Any]]) -> bool:
    """True while the run is ``pending`` or ``running``."""
    return run_status(record) in ACTIVE


def finished_status(exit_code: int) -> RunStatus:
    """The terminal state an executor records for its child's exit code."""
    return RunStatus.SUCCESS if exit_code == 0 else RunStatus.FAILED
