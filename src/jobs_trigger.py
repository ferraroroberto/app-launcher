"""The ``--trigger`` vocabulary shared by every Jobs-tab fire path.

One job executor (:class:`~app.cli.commands.run_job_cmd.RunJobCommand`)
serves four callers, each of which mints a trigger string and forwards it
onto argv via :func:`src.jobs_schtasks.spawn_run_job_detached`:

* ``"scheduled"`` — Windows Task Scheduler.
* ``"manual"``    — ``POST /api/jobs/<id>/run`` (phone tap / Stream Deck),
  and the mutex-queue drain's fallback when an entry carries no trigger.
* ``"webhook"``   — ``POST /api/jobs/<id>/hook`` (issue #73).
* ``"chain:<upstream_job_id>"`` — a DAG ``on_success`` / ``on_failure``
  consequence (issue #68), dispatched by :mod:`src.jobs_queue`.

This module is the single source of that vocabulary. It exists because
the mint sites and the argparse declaration that has to accept them live
in different layers: when the executor's ``--trigger`` was declared as
``choices=["scheduled", "manual"]`` the two *constructed* values
(``chain:<id>`` and ``webhook``) were rejected by argparse, which exits 2
before the command body ever runs. Since the spawn is fire-and-forget
(nothing waits on the child's exit code) and the pre-written ``run.json``
never got a ``pid``, the run was silently orphaned — unreapable by
:mod:`src.jobs_reap`, which needs a pid — so both features looked wired
end to end while never having fired once (issue #687).

Stdlib-only and dependency-free on purpose, so both the ``src`` mint
sites and the ``app.cli`` parser can import it without a cycle.
"""

from __future__ import annotations

from typing import Optional, Tuple

#: Prefix marking a DAG chain fire; the remainder is the upstream job id.
CHAIN_TRIGGER_PREFIX = "chain:"

#: Trigger values that stand alone, with no payload attached.
SIMPLE_TRIGGERS: Tuple[str, ...] = ("scheduled", "manual", "webhook")

#: Human-readable shape, for argparse help/error text.
TRIGGER_SYNTAX = (
    "one of " + ", ".join(SIMPLE_TRIGGERS) + f", or {CHAIN_TRIGGER_PREFIX}<upstream_job_id>"
)


def chain_trigger(upstream_id: str) -> str:
    """Build the trigger string for a fire chained off ``upstream_id``."""
    return f"{CHAIN_TRIGGER_PREFIX}{upstream_id}"


def chain_upstream_id(trigger: str) -> Optional[str]:
    """The upstream job id carried by a ``chain:<id>`` trigger.

    Returns ``None`` for any other trigger, and for a bare ``"chain:"``
    with no id after the prefix (which is not a valid chain trigger).
    """
    if not trigger.startswith(CHAIN_TRIGGER_PREFIX):
        return None
    return trigger[len(CHAIN_TRIGGER_PREFIX) :] or None


def is_valid_trigger(trigger: str) -> bool:
    """Whether ``trigger`` is a value the executor is allowed to receive."""
    return trigger in SIMPLE_TRIGGERS or chain_upstream_id(trigger) is not None
