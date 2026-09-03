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
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, Iterator, List, Optional

from src._json_io import atomic_write_json, file_lock
from src.jobs_argv import ArgvRejected
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
from src.jobs_trigger import chain_trigger

logger = logging.getLogger(__name__)

JOBS_QUEUE_PATH = JOBS_RUNS_DIR / "_queue.json"
# In-process fast-path (cheap, avoids taking the file lock for same-process
# contention); the file lock below is what actually serializes the
# read-modify-write across processes.
_queue_lock = Lock()


@contextmanager
def _queue_file_lock() -> Iterator[None]:
    """Hold an exclusive interprocess lock for a queue-file read-modify-write.

    Enqueues can originate from genuinely separate OS processes — the
    webapp process and a spawned ``run-job`` executor process — so the
    in-process :data:`_queue_lock` alone doesn't prevent two writers from
    reading the same pre-write state and one clobbering the other's
    ``os.replace`` (issue #409). Thin wrapper around the shared
    :func:`src._json_io.file_lock` (same sidecar-lock pattern as
    ``src.jobs_history._run_json_lock``).

    Resolves the lock path from the current :data:`JOBS_QUEUE_PATH` value
    on every call (rather than caching it at import time) so tests that
    ``monkeypatch`` ``JOBS_QUEUE_PATH`` to a tmp dir redirect the lock file
    too, instead of touching the real production runs dir.
    """
    lock_path = JOBS_QUEUE_PATH.parent / (JOBS_QUEUE_PATH.name + ".lock")
    with file_lock(lock_path, label="mutex queue"):
        yield


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
    """Append ``entry`` to the FIFO under ``group``.

    Holds the in-process lock (fast-path for same-process races) nested
    inside the interprocess file lock (:func:`_queue_file_lock`), which is
    what actually serializes the read-modify-write across the webapp
    process and a spawned executor process (issue #409).
    """
    with _queue_lock, _queue_file_lock():
        state = _read_queue_file()
        state.setdefault(group, []).append(entry)
        _write_queue_file(state)


def pop_mutex_entry(group: str) -> Optional[Dict[str, Any]]:
    """Atomically pop and return the head of ``group``'s queue, or
    ``None`` when the queue is empty / missing.
    """
    with _queue_lock, _queue_file_lock():
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
    with _queue_lock, _queue_file_lock():
        return list(_read_queue_file().get(group) or [])


def mutex_collision(jobs: List[Job], job: Job) -> Optional[Job]:
    """Return the *other* job in ``job.mutex_group`` that currently holds
    the group (latest run is ``running`` or ``pending``), or ``None``.

    Shared by the route's admission gate and the chain dispatcher so a
    chain-fired downstream gets the same queue-if-busy treatment as a
    manual fire.
    """
    if not job.mutex_group:
        return None
    # Local import avoids a jobs_queue <-> jobs_reap import cycle
    # (jobs_reap.reap_stranded_runs locally imports drain_mutex_queue from
    # here) — same pattern as write_run_json's local jobs_index import.
    # finalize_dead_runs (not reap_stranded_runs) deliberately skips the
    # mutex-queue drain: we're still mid-sweep deciding admission for
    # `job` itself, and draining here could spawn a queued sibling before
    # the rest of this same loop has re-checked it against that spawn.
    from src.jobs_reap import finalize_dead_runs

    for other in jobs:
        if other.id == job.id:
            continue
        if other.mutex_group != job.mutex_group:
            continue
        # A stranded "running" record (executor died before finalising,
        # issue #591) must not wedge the group forever — reconcile it
        # before treating it as a live collision.
        finalize_dead_runs(other)
        latest = latest_run(other.id)
        if latest is None:
            continue
        if latest.get("status") in ("running", "pending"):
            return other
    return None


def seed_run_meta(
    job: Job,
    trigger: str,
    started_at: str,
    *,
    run_id: str,
    **extra: Any,
) -> Dict[str, Any]:
    """Build the common seed fields for a fresh ``run.json`` record.

    Every real fire path (manual / webhook / scheduled / chain / dry-run)
    writes the same seven fields — ``run_id`` / ``job_id`` / ``name`` /
    ``trigger`` / ``script_path`` / ``args`` / ``started_at`` — before
    layering on its own status/provenance fields. Hand-copied in 8 places
    across 3 modules before issue #794; this is the one place that shape
    lives now. ``run_id`` is required (the caller already created the run
    dir and knows its name); ``**extra`` merges on top, so a caller can
    pass e.g. ``status="running"`` or ``chained_from=upstream_id`` directly
    into the returned dict.
    """
    meta: Dict[str, Any] = dict(
        run_id=run_id,
        job_id=job.id,
        name=job.name,
        trigger=trigger,
        script_path=job.script_path,
        args=job.args,
        started_at=started_at,
    )
    meta.update(extra)
    return meta


def admit_and_spawn(
    jobs: List[Job],
    job: Job,
    trigger: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    extra_meta: Optional[Dict[str, Any]] = None,
    on_run_dir: Optional[Callable[[Path], None]] = None,
    spawn_when_free: bool = True,
    spawn_error_key: str = "spawn_error",
) -> Optional[Dict[str, Any]]:
    """Mutex admission for one fire: queue it behind a live sibling, or
    create the run and spawn it detached.

    The one implementation of the admission block that used to exist three
    times in parallel — the webapp route, the chain dispatcher, and the
    executor's own scheduled-fire gate (issue #794). They had already
    drifted: ``dispatch_chain_run`` wrote ``status="failed"`` to disk but
    returned a meta dict still claiming ``"pending"``. A single status
    source means that class of drift cannot recur.

    Both branches pre-create the run dir, so the caller always gets a real
    ``run_id`` back:

    * **collision** — ``status="queued"`` plus ``mutex_group`` /
      ``mutex_blocked_by``, pushed onto the group's FIFO. The executor
      finalising the holder pops and spawns it (:func:`drain_mutex_queue`).
    * **free** — ``status="pending"``, then :func:`spawn_run_job_detached`.
      An ``OSError`` (the spawn itself failed) or a ``ValueError`` (the argv
      was refused before the spawn — see
      :func:`src.jobs_argv.reject_cmd_unsafe`) is recorded on the run as
      ``status="failed"`` / ``exit_code=-1`` plus the reason under
      ``spawn_error_key``, so the UI surfaces the failure instead of a stuck
      ``pending``. Both are "this fire produced no child": the run dir is
      already on disk by then, so letting either escape would leave exactly
      the orphaned ``pending`` record this helper exists to prevent. The
      meta also carries ``refused`` (issue #810): ``True`` for the
      :class:`~src.jobs_argv.ArgvRejected` case, ``False`` for a genuine
      ``OSError``, so an HTTP caller can answer 400 vs 500 instead of
      collapsing both into "the server broke".

    ``spawn_when_free=False`` is the executor's case: it is *already* the
    process that will run this job inline, so it only wants the queue half.
    With no collision it returns ``None`` having touched nothing — no run
    dir, no record — and the caller proceeds to run normally.

    Deliberately *not* absorbed here, because they genuinely differ per
    call site: cooldown admission (route-only — a chain or scheduled fire
    is an explicit consequence, not user mashing), the executor's "has this
    already been through a gate?" scoping, and the route's HTTP error
    translation.

    Returns the ``run.json`` metadata as written (``status`` one of
    ``queued`` / ``pending`` / ``failed``), or ``None`` in the
    ``spawn_when_free=False``-with-no-collision case.
    """
    holder = mutex_collision(jobs, job)
    if holder is None and not spawn_when_free:
        return None

    run_dir = new_run_dir(job.id, new_run_id())
    if on_run_dir is not None:
        on_run_dir(run_dir)
    meta = seed_run_meta(
        job,
        trigger,
        datetime.now().isoformat(timespec="seconds"),
        run_id=run_dir.name,
        **(extra_meta or {}),
    )
    if params:
        meta["params"] = params

    if holder is not None:
        meta.update(
            status="queued",
            mutex_group=job.mutex_group,
            mutex_blocked_by=holder.id,
        )
        write_run_json(run_dir, **meta)
        enqueue_mutex(
            job.mutex_group,
            {
                "job_id": job.id,
                "run_id": run_dir.name,
                "trigger": trigger,
                "params": params or None,
            },
        )
        logger.info(
            f"🪢 queued {job.id}/{run_dir.name} behind {holder.id} "
            f"(mutex_group={job.mutex_group!r}, trigger={trigger!r})"
        )
        return meta

    meta["status"] = "pending"
    write_run_json(run_dir, **meta)
    try:
        spawn_run_job_detached(job.id, run_dir.name, trigger, params or None)
    except (OSError, ValueError) as exc:
        # Reflect the failure in the *returned* meta too, not just on disk
        # (issue #794) — a caller reading the return value used to see a
        # stale status="pending" even though run.json already said "failed".
        failure: Dict[str, Any] = {
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "exit_code": -1,
            "status": "failed",
            spawn_error_key: str(exc),
            "refused": isinstance(exc, ArgvRejected),
        }
        meta.update(failure)
        write_run_json(run_dir, **failure)
        logger.warning(
            f"⚠️  spawn failed {job.id}/{run_dir.name} "
            f"(trigger={trigger!r}): {exc}"
        )
        return meta

    logger.info(f"🚀 fired {job.id}/{run_dir.name} (trigger={trigger!r})")
    return meta


def dispatch_chain_run(
    jobs: List[Job], downstream: Job, upstream_id: str
) -> Dict[str, Any]:
    """Fire ``downstream`` as a chain consequence of ``upstream_id``.

    Thin wrapper over :func:`admit_and_spawn` — the same mutex admission
    the route's ``POST /api/jobs/<id>/run`` uses — recording the upstream
    run on the record via ``chained_from``. Returns the metadata that
    ended up in ``run.json`` so the caller can log or surface it.

    Cooldown is intentionally NOT checked — chain fires are an explicit
    downstream consequence, not a user click. (The executor only
    cooldown-skips ``scheduled`` triggers, mirroring this policy from
    the other side: a chain trigger ``chain:<id>`` reaches the executor
    and runs straight through.)
    """
    meta = admit_and_spawn(
        jobs,
        downstream,
        chain_trigger(upstream_id),
        extra_meta={"chained_from": upstream_id},
        spawn_error_key="chain_spawn_error",
    )
    if meta is None:
        # Unreachable: ``None`` is the ``spawn_when_free=False`` case alone,
        # and a chain fire always spawns when the group is free. Raise
        # rather than fabricate a record — a chain hop with no run behind it
        # is a bug, not an empty result.
        raise RuntimeError(
            f"admit_and_spawn produced no run record for chain fire "
            f"{downstream.id} (upstream={upstream_id})"
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
    except (OSError, ValueError) as exc:
        # ValueError as well as OSError, for the same reason `admit_and_spawn`
        # catches both: a refused argv (src.jobs_argv.reject_cmd_unsafe) is
        # "this fire produced no child", not a bug to unwind through. It
        # reaches this path only when the fire was *queued* behind a mutex
        # holder, because the queued branch enqueues without ever attempting
        # a spawn — so the refusal lands here at drain time instead. Letting
        # it escape would surface it wherever the drain is called from: the
        # kill route and the reaper, neither of which is the fire's caller.
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
