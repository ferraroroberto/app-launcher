"""Facade over the Jobs-tab backend (issue #315 split).

This module used to own everything itself; it now just re-exports the
public surface of four focused modules so existing callers
(``from src.jobs import X`` / ``from src import jobs as jobs_mod;
jobs_mod.X(...)``) keep working unchanged:

* :mod:`src.jobs_schtasks` — Windows Task Scheduler sync + spawn helpers,
  and the deterministic next-fire arithmetic.
* :mod:`src.jobs_history` — run-history file storage (``run.json`` /
  ``output.log`` under ``webapp/jobs/<job_id>/<run_id>/``).
* :mod:`src.jobs_queue` — the cross-job mutex queue + chain dispatch.
* :mod:`src.jobs_stats` — run-duration percentiles, health, stuck-run
  detection.

New code should generally import directly from the module that actually
owns what it needs; this facade exists for backward compatibility and as
a convenient one-stop import for call sites that touch several of the
above (e.g. the webapp's jobs router).

Tests that need to patch module-internal state (e.g. ``JOBS_RUNS_DIR``,
``_run_schtasks``, ``spawn_run_job_detached`` as the mutex queue's default
spawn callable) must monkeypatch the module that actually *owns* that
state — ``src.jobs_history``, ``src.jobs_schtasks``, or ``src.jobs_queue``
respectively — not this facade. Patching a name re-exported here only
rebinds the facade's copy of the reference; it has no effect on the
owning module's own globals, which is what the real code reads.

The single executor that ever runs a job is
:class:`~app.cli.commands.run_job_cmd.RunJobCommand`. Task Scheduler
calls it with ``pythonw launcher.py run-job <id>``; the webapp's
``POST /api/jobs/<id>/run`` route spawns the same command detached and
returns the new ``run_id`` immediately.
"""

from __future__ import annotations

from src.jobs_history import (
    JOBS_RUNS_DIR,
    MAX_RUNS_PER_JOB,
    is_running,
    latest_run,
    list_runs,
    new_run_dir,
    new_run_id,
    prune_runs,
    read_output_tail,
    read_run,
    runs_dir,
    write_run_json,
)
from src.jobs_queue import (
    JOBS_QUEUE_PATH,
    dispatch_chain_run,
    drain_mutex_queue,
    enqueue_mutex,
    mutex_collision,
    peek_mutex_queue,
    pop_mutex_entry,
    remove_queue_entry,
)
from src.jobs_schtasks import (
    FREQUENT_SCHEDULE_TYPES,
    TASK_FOLDER_PREFIX,
    TASK_NAMESPACE,
    _run_schtasks,  # noqa: F401 -- re-exported for tests that monkeypatch it here
    delete_schtasks,
    invalidate_next_run_cache,
    list_known_tasks,
    next_fire,
    query_next_run,
    resolve_venv_python,
    schedule_argv_parts,
    spawn_run_job_detached,
    sync_schtasks,
    task_names_for,
    task_run_command,
    upcoming_fires,
)
from src.jobs_stats import (
    consecutive_failed_runs,
    cooldown_check,
    invalidate_stats_cache,
    is_stuck,
    run_stats,
)

__all__ = [
    "JOBS_RUNS_DIR",
    "MAX_RUNS_PER_JOB",
    "JOBS_QUEUE_PATH",
    "TASK_NAMESPACE",
    "TASK_FOLDER_PREFIX",
    "FREQUENT_SCHEDULE_TYPES",
    "resolve_venv_python",
    "task_run_command",
    "spawn_run_job_detached",
    "task_names_for",
    "schedule_argv_parts",
    "list_known_tasks",
    "delete_schtasks",
    "sync_schtasks",
    "invalidate_next_run_cache",
    "query_next_run",
    "next_fire",
    "upcoming_fires",
    "runs_dir",
    "new_run_id",
    "new_run_dir",
    "write_run_json",
    "read_run",
    "list_runs",
    "latest_run",
    "is_running",
    "prune_runs",
    "read_output_tail",
    "enqueue_mutex",
    "pop_mutex_entry",
    "peek_mutex_queue",
    "remove_queue_entry",
    "mutex_collision",
    "dispatch_chain_run",
    "drain_mutex_queue",
    "run_stats",
    "invalidate_stats_cache",
    "is_stuck",
    "consecutive_failed_runs",
    "cooldown_check",
]
