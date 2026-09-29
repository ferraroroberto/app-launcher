"""In-memory job runtime snapshot behind ``/api/jobs`` and ``/api/board`` (#1324).

Both polls used to walk every job's run history on every request: reap
stranded runs, then ``latest_run``, each a full ``list_runs`` (``iterdir``
plus a ``run.json`` read per retained run), ~1,100 file reads per poll across
32 jobs. Under disk contention that made the Jobs tab take 8 s to open. This
module does that walk once, off the request path, and the two handlers read
the result.

**Freshness.** A background tick (:func:`tick_forever`, started from the
webapp lifespan) rebuilds every :data:`TICK_SECONDS` while someone is asking
(a read within :data:`DEMAND_WINDOW_SECONDS`), and idles otherwise, so an
unwatched launcher does no walking at all. A run the webapp itself writes
(start, kill, pin, reap) marks its job dirty through
:func:`mark_dirty`, called from ``jobs_history.write_run_json``, and the next
read recomputes just that job. A run the detached executor finishes in its
own process shows up on the next tick.

**Never old state as current.** A read that finds the snapshot older than
:data:`MAX_AGE_SECONDS` (the first read after an idle spell, or a tick that
has died) rebuilds inline before answering, and says so in the log. Every
answer carries its age (:func:`age_seconds`), which the handlers expose.

**Per-job errors.** A job whose history can't be read keeps the ``OSError``
in its entry instead of a result. The jobs route then recomputes that job
inline, raising exactly as before. The Board turns it into its
``unreadable`` card.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, Optional, Set

logger = logging.getLogger(__name__)

TICK_SECONDS = 3.0
DEMAND_WINDOW_SECONDS = 60.0
MAX_AGE_SECONDS = 10.0
# A rebuild slower than this leaves an info breadcrumb, so a regression in
# the walk shows up in the log before it shows up on the phone.
SLOW_BUILD_SECONDS = 2.0


@dataclass
class JobRuntime:
    """One job's walked state: what the two handlers used to compute per poll."""

    latest: Optional[Dict[str, Any]] = None
    stats: Optional[Dict[str, Any]] = None
    stuck: bool = False
    coverage: Optional[Dict[str, Any]] = None
    error: Optional[OSError] = None


@dataclass
class Snapshot:
    built_monotonic: float
    built_at: str
    jobs: Dict[str, JobRuntime] = field(default_factory=dict)


_lock = threading.Lock()
# Serializes builds: a read that must rebuild waits for a tick already
# building instead of walking the same disk twice.
_build_lock = threading.Lock()
_snapshot: Optional[Snapshot] = None
_dirty: Set[str] = set()
_last_demand = 0.0


def compute_job(job: Any) -> JobRuntime:
    """Walk one job's history: reap, latest run, stats, stuck, coverage.

    The only place the GET path's old per-job walk still happens. Blocking.
    """
    from src import jobs as jobs_mod

    # One job's unreadable history must not sink the whole snapshot (and with
    # it every other job on the Board), so any read error is kept per job.
    try:
        # A run stranded "running" by a dead executor (issue #591) is
        # reconciled here, as both polls used to do on every request.
        jobs_mod.reap_stranded_runs(job)
        latest = jobs_mod.latest_run(job.id)
        runtime = JobRuntime(
            latest=latest,
            stats=jobs_mod.run_stats(job.id),
            stuck=jobs_mod.is_stuck(job.id, latest=latest),
            coverage=jobs_mod.coverage_for_job(job.id),
        )
    except OSError as exc:
        return JobRuntime(error=exc)
    # Warm the shared schtasks cache the jobs route reads per request, so its
    # 30 s expiry is paid here rather than by a poll.
    jobs_mod.query_next_run(job.id)
    return runtime


def _load_jobs() -> Iterable[Any]:
    from src.jobs_config import load_jobs

    return load_jobs().jobs


def rebuild(reason: str, *, unless_younger_than: Optional[float] = None) -> Snapshot:
    """Walk every job and swap the snapshot in. Blocking.

    With ``unless_younger_than``, a snapshot that became that fresh while
    this call waited for the build lock (a tick finishing first) is returned
    as-is instead of walking the disk a second time.
    """
    global _snapshot
    with _build_lock:
        if unless_younger_than is not None:
            with _lock:
                existing = _snapshot
            if existing is not None and (
                time.monotonic() - existing.built_monotonic <= unless_younger_than
            ):
                return existing
        start = time.monotonic()
        with _lock:
            _dirty.clear()
        jobs = {job.id: compute_job(job) for job in _load_jobs()}
        snap = Snapshot(
            built_monotonic=time.monotonic(),
            built_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            jobs=jobs,
        )
        with _lock:
            _snapshot = snap
        elapsed = snap.built_monotonic - start
    if reason != "tick" or elapsed >= SLOW_BUILD_SECONDS:
        logger.info(
            "ℹ️ jobs snapshot rebuilt (%s): %d jobs in %.2fs", reason, len(jobs), elapsed,
        )
    return snap


def mark_dirty(job_id: str) -> None:
    """Recompute ``job_id`` on the next read: its run.json just changed here."""
    with _lock:
        _dirty.add(job_id)


def current(jobs: Iterable[Any]) -> Snapshot:
    """The snapshot to answer a request from, never older than MAX_AGE_SECONDS.

    ``jobs`` is the registry the request is decorating. A job missing from
    the snapshot (just created) or marked dirty is recomputed before
    answering. Blocking — callers are already off the event loop.
    """
    global _last_demand, _snapshot
    now = time.monotonic()
    with _lock:
        _last_demand = now
        snap = _snapshot
    if snap is None or now - snap.built_monotonic > MAX_AGE_SECONDS:
        snap = rebuild(
            "first read" if snap is None else "stale on read",
            unless_younger_than=MAX_AGE_SECONDS,
        )
    with _lock:
        stale_ids = set(_dirty)
        _dirty.difference_update(stale_ids)
    missing = [job for job in jobs if job.id in stale_ids or job.id not in snap.jobs]
    if missing:
        updates = {job.id: compute_job(job) for job in missing}
        with _lock:
            # Copy-on-write: a reader holding the old mapping keeps a
            # consistent view.
            snap = Snapshot(snap.built_monotonic, snap.built_at, {**snap.jobs, **updates})
            if _snapshot is not None and _snapshot.built_monotonic == snap.built_monotonic:
                _snapshot = snap
    return snap


def age_seconds(snap: Snapshot) -> float:
    return round(max(0.0, time.monotonic() - snap.built_monotonic), 1)


def describe(snap: Snapshot) -> Dict[str, Any]:
    """The staleness fields both handlers attach to their response."""
    return {"built_at": snap.built_at, "age_seconds": age_seconds(snap)}


def _demanded() -> bool:
    with _lock:
        return time.monotonic() - _last_demand <= DEMAND_WINDOW_SECONDS


async def tick_forever() -> None:
    """Rebuild every TICK_SECONDS while the polls are asking; idle otherwise."""
    while True:
        await asyncio.sleep(TICK_SECONDS)
        if not _demanded():
            continue
        try:
            await asyncio.to_thread(rebuild, "tick")
        except Exception:  # noqa: BLE001 — a failed tick must not end the loop
            logger.exception("❌ jobs snapshot tick failed; reads will rebuild inline")


def reset() -> None:
    """Drop all state. Tests only."""
    global _snapshot, _last_demand
    with _lock:
        _snapshot = None
        _dirty.clear()
        _last_demand = 0.0
