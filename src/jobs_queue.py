"""Cross-job mutex queue for the Jobs tab (issue #68 PR #2).

When a job carries a ``mutex_group`` and another job in the same group is
``running`` or ``pending``, the fresh fire is queued rather than rejected.
The finalising executor pops the next entry on its way out and spawns it
detached. Queue file lives at :data:`JOBS_QUEUE_PATH` (one JSON document,
keyed by group → FIFO list of pending entries).

Split out of :mod:`src.jobs` (issue #315) — run-history file storage lives
in :mod:`src.jobs_history`, schtasks sync + spawn helpers in
:mod:`src.jobs_schtasks`, and percentiles/health in :mod:`src.jobs_stats`.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from threading import Lock
from typing import Any, Callable, Dict, List, Optional

from src._json_io import atomic_write_json
from src.jobs_config import Job
from src.jobs_history import (
    JOBS_RUNS_DIR,
    latest_run,
    new_run_dir,
    new_run_id,
    read_run,
    runs_dir,
    write_run_json,
)
from src.jobs_schtasks import spawn_run_job_detached

logger = logging.getLogger(__name__)

JOBS_QUEUE_PATH = JOBS_RUNS_DIR / "_queue.json"
_queue_lock = Lock()


def _read_queue_file() -> Dict[str, List[Dict[str, Any]]]:
    """Read the on-disk queue. Missing file → empty queue."""
    if not JOBS_QUEUE_PATH.is_file():
        return {}
    try:
        data = json.loads(JOBS_QUEUE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            f"⚠️  mutex queue file unreadable ({exc}); treating as empty"
        )
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, List[Dict[str, Any]]] = {}
    for group, entries in data.items():
        if not isinstance(group, str) or not isinstance(entries, list):
            continue
        out[group] = [e for e in entries if isinstance(e, dict)]
    return out


def _write_queue_file(state: Dict[str, List[Dict[str, Any]]]) -> None:
    """Persist the queue atomically. Drops groups whose list is empty."""
    JOBS_QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    pruned = {g: e for g, e in state.items() if e}
    atomic_write_json(JOBS_QUEUE_PATH, pruned)


def enqueue_mutex(group: str, entry: Dict[str, Any]) -> None:
    """Append ``entry`` to the FIFO under ``group``. Holds a process lock
    around the read-modify-write so two route handlers in the same
    process can't race; the lock is process-local but ``os.replace`` is
    OS-atomic, so cross-process races at worst dedupe via the spawn-time
    ``is_running``/``status`` guards (see :func:`drain_mutex_queue`).
    """
    with _queue_lock:
        state = _read_queue_file()
        state.setdefault(group, []).append(entry)
        _write_queue_file(state)


def pop_mutex_entry(group: str) -> Optional[Dict[str, Any]]:
    """Atomically pop and return the head of ``group``'s queue, or
    ``None`` when the queue is empty / missing.
    """
    with _queue_lock:
        state = _read_queue_file()
        entries = state.get(group) or []
        if not entries:
            return None
        head = entries[0]
        state[group] = entries[1:]
        _write_queue_file(state)
        return head


def peek_mutex_queue(group: str) -> List[Dict[str, Any]]:
    """Read-only snapshot of ``group``'s queue. Defensive copy."""
    with _queue_lock:
        return list(_read_queue_file().get(group) or [])


def remove_queue_entry(group: str, run_id: str) -> bool:
    """Remove a queued entry by ``run_id``. Returns ``True`` when removed."""
    with _queue_lock:
        state = _read_queue_file()
        entries = state.get(group) or []
        keep = [e for e in entries if e.get("run_id") != run_id]
        if len(keep) == len(entries):
            return False
        state[group] = keep
        _write_queue_file(state)
        return True


def mutex_collision(jobs: List[Job], job: Job) -> Optional[Job]:
    """Return the *other* job in ``job.mutex_group`` that currently holds
    the group (latest run is ``running`` or ``pending``), or ``None``.

    Shared by the route's admission gate and the chain dispatcher so a
    chain-fired downstream gets the same queue-if-busy treatment as a
    manual fire.
    """
    if not job.mutex_group:
        return None
    for other in jobs:
        if other.id == job.id:
            continue
        if other.mutex_group != job.mutex_group:
            continue
        latest = latest_run(other.id)
        if latest is None:
            continue
        if latest.get("status") in ("running", "pending"):
            return other
    return None


def dispatch_chain_run(
    jobs: List[Job], downstream: Job, upstream_id: str
) -> Dict[str, Any]:
    """Fire ``downstream`` as a chain consequence of ``upstream_id``.

    Pre-creates the run dir, runs the same mutex admission as the
    route's POST /api/jobs/<id>/run, and either spawns detached or
    enqueues. Returns the metadata that ended up in ``run.json`` so the
    caller can log or surface it.

    Cooldown is intentionally NOT checked — chain fires are an explicit
    downstream consequence, not a user click. (The executor only
    cooldown-skips ``scheduled`` triggers, mirroring this policy from
    the other side: a chain trigger ``chain:<id>`` reaches the executor
    and runs straight through.)
    """
    holder = mutex_collision(jobs, downstream)
    run_dir = new_run_dir(downstream.id, new_run_id())
    started_at = datetime.now().isoformat(timespec="seconds")
    trigger = f"chain:{upstream_id}"
    meta: Dict[str, Any] = dict(
        run_id=run_dir.name,
        job_id=downstream.id,
        name=downstream.name,
        trigger=trigger,
        script_path=downstream.script_path,
        args=downstream.args,
        started_at=started_at,
        chained_from=upstream_id,
    )
    if holder is not None:
        meta["status"] = "queued"
        meta["mutex_group"] = downstream.mutex_group
        meta["mutex_blocked_by"] = holder.id
        write_run_json(run_dir, **meta)
        enqueue_mutex(
            downstream.mutex_group,
            {
                "job_id": downstream.id,
                "run_id": run_dir.name,
                "trigger": trigger,
                "params": None,
            },
        )
        logger.info(
            f"🪢🪡 chain queued {downstream.id}/{run_dir.name} behind "
            f"{holder.id} (mutex_group={downstream.mutex_group!r}, "
            f"upstream={upstream_id})"
        )
        return meta
    meta["status"] = "pending"
    write_run_json(run_dir, **meta)
    try:
        spawn_run_job_detached(downstream.id, run_dir.name, trigger, None)
    except OSError as exc:
        write_run_json(
            run_dir,
            finished_at=datetime.now().isoformat(timespec="seconds"),
            exit_code=-1,
            status="failed",
            chain_spawn_error=str(exc),
        )
        logger.warning(
            f"⚠️  chain spawn failed {downstream.id}/{run_dir.name}: {exc}"
        )
        return meta
    logger.info(
        f"🪡 chain fired {downstream.id}/{run_dir.name} "
        f"(upstream={upstream_id})"
    )
    return meta


def drain_mutex_queue(
    group: str,
    *,
    spawn: Optional[
        Callable[[str, str, str, Optional[Dict[str, Any]]], int]
    ] = None,
) -> Optional[Dict[str, Any]]:
    """Pop one queued entry for ``group`` and spawn it detached.

    Designed to be called by the finalising executor (and the kill
    endpoint) so a mutex group never wedges with a still-queued entry
    after the head run completes. Returns the entry that was spawned, or
    ``None`` when the queue was empty.

    Defensive double-spawn guard: the picked entry's run-dir must still
    have ``status == "queued"``. If it's already running/success/failed
    a concurrent finaliser raced us — we skip the spawn but log and
    leave the queue otherwise untouched (the head was already popped).
    """
    entry = pop_mutex_entry(group)
    if entry is None:
        return None
    job_id = entry.get("job_id")
    run_id = entry.get("run_id")
    if not isinstance(job_id, str) or not isinstance(run_id, str):
        logger.warning(
            f"⚠️  mutex queue {group!r}: dropping malformed entry {entry!r}"
        )
        return None
    run_dir = runs_dir(job_id) / run_id
    record = read_run(run_dir)
    if record.get("status") != "queued":
        logger.warning(
            f"⚠️  mutex queue {group!r}: head {job_id}/{run_id} status "
            f"{record.get('status')!r} (expected 'queued'); skipping spawn"
        )
        return None
    trigger = str(entry.get("trigger") or "manual")
    params = entry.get("params") if isinstance(entry.get("params"), dict) else None
    fn = spawn or spawn_run_job_detached
    try:
        fn(job_id, run_id, trigger, params)
    except OSError as exc:
        logger.error(
            f"❌ mutex queue {group!r}: spawn failed for {job_id}/{run_id}: {exc}"
        )
        # Don't re-enqueue — the run dir already exists with status=queued
        # and the operator can re-fire manually. Refusing to retry blindly
        # keeps a misconfigured job from spinning forever.
        return None
    logger.info(
        f"🪢 mutex queue {group!r}: spawned next run {job_id}/{run_id} "
        f"(trigger={trigger})"
    )
    return entry
