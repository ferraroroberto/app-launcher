"""A Board job or CI attention card's ``state``, declared once.

The Board's "PRs and jobs" lane carries two server-built card kinds that ask a
human to look: a ``job`` card (built by ``src/board/__init__.py`` from the jobs
runtime snapshot) and a ``ci`` card (built by ``src/board/ci_runs.py`` from the
CI sweep). Their ``state`` field is one closed set, and the two groupings the
Board needs live here so the browser copy
(``app/webapp/static/board/attention-state.js``) and the server agree:

* ``failed``, ``unconfirmed``: a finished run, a past event; a job card in one
  of them may be dismissed once dealt with.
* ``stuck``, ``unreadable``: a live problem, never dismissible; it leaves only
  when the run finishes or its history reads again.
* ``unknown``: a CI repo whose runs could not be read, never drawn as green.

No card moves between states: every poll rebuilds the card from its source, so
the rule this machine enforces is which states a reader may act on, not a
transition order.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Optional

from src.jobs.outcome import OUTCOME_UNCONFIRMED


class AttentionState(StrEnum):
    """Every value a Board job or CI attention card's ``state`` may hold."""

    #: A job run that failed today, or a red CI workflow on a default branch.
    FAILED = "failed"
    #: A job run whose delivery the scheduled-run adapter could not establish.
    UNCONFIRMED = "unconfirmed"
    #: A job run still marked running past its stuck threshold.
    STUCK = "stuck"
    #: A job whose run history could not be read.
    UNREADABLE = "unreadable"
    #: A CI repo whose runs could not be read.
    UNKNOWN = "unknown"


#: Counts toward the Board's "N failing" (the browser copy groups the same way): a failure, or a run that will not finish.
FAILING = frozenset({AttentionState.FAILED, AttentionState.STUCK})
#: A past event the owner may dismiss from a job card; the rest describe a live problem.
DISMISSIBLE = frozenset({AttentionState.FAILED, AttentionState.UNCONFIRMED})


def finished_run_state(outcome: Optional[str]) -> AttentionState:
    """The state a failed-today job run's card takes from the run's decorated ``outcome``.

    An unconfirmed run still wants a human's eye, but it is not a failure and
    must not be drawn as one.
    """
    return AttentionState.UNCONFIRMED if outcome == OUTCOME_UNCONFIRMED else AttentionState.FAILED

