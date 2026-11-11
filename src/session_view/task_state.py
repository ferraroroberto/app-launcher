"""A session background task's state, declared once.

A task the agent started with ``run_in_background`` or ``Monitor`` is in one
of two phases. It is RUNNING from its launch until Claude Code queues a
``<task-notification>`` that carries a ``<status>``; that first status
finishes it and is final. A monitor's per-event notifications carry no
status and leave the task running, and a later status never re-opens or
rewrites a finished task.

The status itself is Claude Code's word (``completed``, ``failed``,
``killed``, ``stopped``, ``timeout``), an open set the sheet shows as
written; one that is not a plain word is recorded as ``finished``.
:func:`next_status` is the one transition rule and :func:`task_phase` the
one reader; ``src/session_view/background_tasks.py`` uses both, and the
browser copy (``app/webapp/static/background-tasks/task-state.js``) shares
the phase values and the success word.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Optional


class TaskPhase(StrEnum):
    """Every phase a background task may be in; a running task's payload status is RUNNING."""

    RUNNING = "running"
    #: A task that ended with a status that is not a plain word.
    FINISHED = "finished"


#: The status Claude Code reports for a plain success; the sheet shows no chip for it.
COMPLETED = "completed"

_STATUS = re.compile(r"^[a-z][a-z_-]{0,19}$")


def next_status(current: Optional[str], reported: str) -> Optional[str]:
    """The status a task holds after a notification that reports ``reported``.

    ``current`` is ``None`` while the task runs. A finished task keeps its
    first status; an empty report (a monitor event) leaves a running task
    running.
    """
    if current is not None or not reported:
        return current
    return reported if _STATUS.match(reported) else TaskPhase.FINISHED.value


def task_phase(status: Optional[str]) -> TaskPhase:
    """The phase of a task whose status is ``status`` (``None`` or RUNNING while it runs)."""
    if status is None or status == TaskPhase.RUNNING:
        return TaskPhase.RUNNING
    return TaskPhase.FINISHED
