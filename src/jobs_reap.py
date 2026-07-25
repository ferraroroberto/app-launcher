"""Reconciliation for stranded ``running``/``pending`` job run records.

A run record is written as ``status: "running"`` before the child spawns and
only reaches a terminal status once the executor's own ``_finalize_run``
(:mod:`app.cli.commands.run_job_cmd`) runs after ``proc.wait()`` returns. If
the *executor itself* dies first — interrupt, reboot, OOM, a parent shell
killing its process tree — nothing ever finalises the record and it stays
``"running"`` forever, even though the child it was tracking is long gone.

This automates what the webapp's kill route already does by hand for that
exact case (``docs/jobs-tab.md`` "Stuck-run kill": *"If the executor has
already exited (orphan pid), the route still finalises the record — the UI
is the authoritative 'is this run done?' surface"*) — it does not invent a
new recovery story, just removes the requirement that a human notice and tap
Kill.

Every non-terminal record in the job's history is checked, not just the
latest — an older "running" record superseded by a newer run is invisible to
every *behavioural* consumer (mutex/streak/run-button all read only the
latest run), but it's still a lie if a user opens that specific historical
run's detail view, so it gets cleaned up too. ``list_runs`` already reads the
whole history on every poll for stats, so this doesn't add I/O beyond what a
poll already pays for.

Two entry points, split so a caller mid-way through an admission decision
never triggers a re-entrant mutex-queue spawn off stale information:

- :func:`finalize_dead_runs` — pid-liveness check + terminal write only.
  Used by ``src.jobs_queue.mutex_collision``, which is itself in the middle
  of deciding whether a *different* job may fire; draining the queue from
  there could spawn a sibling before the rest of that same collision sweep
  has re-checked it.
- :func:`reap_stranded_runs` — the above, plus draining the job's mutex
  queue once if anything was reaped. Safe from a pure read/refresh context
  that isn't deciding an admission itself:
  ``app/webapp/routers/jobs.py::_decorate_job`` (every ``/api/jobs`` poll)
  and ``src.board.jobs_attention``. Mirrors
  :func:`src.app_runtime.prune_dead`'s lazy-on-read pattern rather than a
  background sweep loop.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.diagnostics import is_pid_alive
from src.jobs_config import Job
from src.jobs_history import list_runs, read_run, runs_dir, write_run_json
from src.jobs_stats import invalidate_stats_cache

logger = logging.getLogger(__name__)


def _reap_one(job_id: str, record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Finalise a single non-terminal ``record`` if its pid is provably dead.

    No-ops (returns ``None``) whenever liveness can't be established with
    confidence:

    - no ``pid`` is recorded yet (the tiny window between the record being
      written and the pid being persisted — leave it, the next read will see
      the pid once it lands),
    - the pid is alive, or looks alive and we have no ``pid_create_time`` to
      rule out reuse (a genuinely long-running job must never be reconciled
      out from under itself — Windows recycles pids, so a bare
      ``pid_exists()`` isn't enough once we have a hint to check against).

    On a confirmed-dead pid, writes terminal fields mirroring the kill
    route: ``status: "failed"``, ``finished_at``, ``duration_seconds``,
    marked ``reaped: True`` rather than ``killed: True`` (this was never
    killed, we just lost track of it) — and invalidates the stats cache.
    """
    pid = record.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return None
    if is_pid_alive(pid, record.get("pid_create_time")):
        return None

    run_id = record.get("run_id")
    if not run_id:
        return None
    run_dir = runs_dir(job_id) / str(run_id)

    finished_at = datetime.now().isoformat(timespec="seconds")
    duration_seconds: Optional[float] = None
    started_at = record.get("started_at")
    if isinstance(started_at, str):
        try:
            duration_seconds = (
                datetime.fromisoformat(finished_at)
                - datetime.fromisoformat(started_at)
            ).total_seconds()
        except ValueError:
            duration_seconds = None

    write_run_json(
        run_dir,
        status="failed",
        finished_at=finished_at,
        duration_seconds=duration_seconds,
        reaped=True,
    )
    invalidate_stats_cache(job_id)
    logger.info(
        f"🧟 reaped stranded run {job_id}/{run_id} "
        f"(pid={pid} confirmed dead, executor never finalised)"
    )
    return read_run(run_dir)


def finalize_dead_runs(job: Job) -> List[Dict[str, Any]]:
    """Reap every ``job`` run stuck ``running``/``pending`` with a provably
    dead pid. Does not touch the mutex queue. Returns the reaped records
    (empty when nothing needed reaping).
    """
    reaped: List[Dict[str, Any]] = []
    for record in list_runs(job.id):
        if record.get("status") not in ("running", "pending"):
            continue
        updated = _reap_one(job.id, record)
        if updated is not None:
            reaped.append(updated)
    return reaped


def reap_stranded_runs(job: Job) -> List[Dict[str, Any]]:
    """:func:`finalize_dead_runs`, then drain ``job``'s mutex queue once if
    anything was reaped.

    A reconciled record that was holding a mutex group must drain it too —
    the dead executor will never reach its own ``_drain_mutex_queue_for``,
    so a queued sibling would otherwise stay wedged behind a finalisation
    that's never coming. One drain call is enough regardless of how many
    stranded records were found: it pops exactly one queued entry, same as
    every other finalisation path (a longer queue drains one entry per
    future finalise, same as always). Only call this from a context that
    isn't itself mid-way through deciding whether another job may fire (see
    module docstring) — ``mutex_collision`` uses the drain-less
    :func:`finalize_dead_runs` instead.
    """
    reaped = finalize_dead_runs(job)
    if reaped and job.mutex_group:
        # Local import avoids a jobs_queue <-> jobs_reap import cycle
        # (jobs_queue.mutex_collision imports finalize_dead_runs from here)
        # — same pattern as write_run_json's local jobs_index import.
        from src.jobs_queue import drain_mutex_queue

        drain_mutex_queue(job.mutex_group)
    return reaped
