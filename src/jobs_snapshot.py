"""In-memory job runtime snapshot behind ``/api/jobs`` and ``/api/board`` (#1324).

Both polls used to walk every job's run history on every request: reap
stranded runs, then ``latest_run``, each a full ``list_runs`` (``iterdir``
plus a ``run.json`` read per retained run), ~1,100 file reads per poll across
32 jobs. Under disk contention that made the Jobs tab take 8 s to open. This
module does that walk once, off the request path, and the two handlers read
the result.

**What it holds.** Only what the history walk yields: each job's latest run,
stats and stuck flag, after reaping. The ``schtasks``-backed caches the jobs
route also reads (next run, missed-fire coverage) keep their own TTL caches.
The tick warms them *outside* the build (:func:`warm_caches`), so a slow or
hung ``schtasks`` can never hold up the Board, which never needed it.

**Freshness.** A background tick (:func:`tick_forever`, started from the
webapp lifespan) rebuilds every :data:`TICK_SECONDS` while someone is asking
(a read within :data:`DEMAND_WINDOW_SECONDS`), and idles otherwise, so an
unwatched launcher does no walking at all. A run the webapp itself writes
(start, kill, pin, reap) marks its job dirty through :func:`mark_dirty`,
called from ``jobs_history.write_run_json`` and ``invalidate_stats_cache``,
and the next read recomputes just that job. A run the detached executor
finishes in its own process shows up on the next tick.

**Never old state as current.** A snapshot is stamped with the moment its
walk *started*, so its age is the age of its oldest data. A read that finds
it older than :data:`MAX_AGE_SECONDS` (the first read after an idle spell, or
a tick that has died) rebuilds inline before answering. Every answer carries
its age (:func:`describe`), which both handlers expose.

**Per-job errors.** A job whose history can't be read keeps the exception in
its entry instead of a result. The jobs route then recomputes that job
inline, raising exactly as before. The Board turns it into its
``unreadable`` card.

Its breadcrumbs go to ``webapp/slow-requests.log`` (the ``launcher.slowreq``
logger): the webapp's own stdout is discarded by the tray.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)
# The file-backed breadcrumb logger app/webapp/observability.py attaches.
breadcrumbs = logging.getLogger("launcher.slowreq")

TICK_SECONDS = 3.0
DEMAND_WINDOW_SECONDS = 60.0
MAX_AGE_SECONDS = 10.0
# A rebuild slower than this leaves a breadcrumb, so a regression in the walk
# shows up in the log before it shows up on the phone.
SLOW_BUILD_SECONDS = 2.0
# The tick refreshes the schtasks-backed caches this long before they expire.
WARM_AHEAD_SECONDS = 2 * TICK_SECONDS


@dataclass
class JobRuntime:
    """One job's walked state: what the two handlers used to compute per poll."""

    latest: Optional[Dict[str, Any]] = None
    stats: Optional[Dict[str, Any]] = None
    stuck: bool = False
    error: Optional[Exception] = None


@dataclass
class Snapshot:
    built_monotonic: float
    built_at: str
    jobs: Dict[str, JobRuntime] = field(default_factory=dict)


_lock = threading.Lock()
# Serializes builds: a read that must rebuild waits for a tick already
# building instead of walking the same disk twice. Taken before _lock, never
# while holding it.
_build_lock = threading.Lock()
_snapshot: Optional[Snapshot] = None
# job id -> the sequence number of its latest mark, so a recompute only
# clears the marks it actually covered.
_dirty: Dict[str, int] = {}
_marks = itertools.count(1)
_last_demand = 0.0


def compute_job(job: Any) -> JobRuntime:
    """Walk one job's history: reap, latest run, stats, stuck.

    The only place the polls' old per-job walk still happens. Blocking.
    """
    from src import jobs as jobs_mod

    # One job's unreadable history must not sink the whole snapshot (and with
    # it every other job on the Board), so any error is kept per job.
    try:
        # A run stranded "running" by a dead executor (issue #591) is
        # reconciled here, as both polls used to do on every request.
        jobs_mod.reap_stranded_runs(job)
        latest = jobs_mod.latest_run(job.id)
        return JobRuntime(
            latest=latest,
            stats=jobs_mod.run_stats(job.id),
            stuck=jobs_mod.is_stuck(job.id, latest=latest),
        )
    except Exception as exc:  # noqa: BLE001 — surfaced per job, never swallowed
        return JobRuntime(error=exc)


def _load_jobs() -> Iterable[Any]:
    from src.jobs_config import load_jobs

    return load_jobs().jobs


def rebuild(reason: str, *, unless_younger_than: Optional[float] = None) -> Snapshot:
    """Walk every job and swap the snapshot in. Blocking.

    With ``unless_younger_than``, a snapshot that is already that fresh (a
    tick finishing while this call waited for the build lock) is returned
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
        # Stamped before the walk: the snapshot is as old as its oldest data.
        start = time.monotonic()
        built_at = datetime.now().astimezone().isoformat(timespec="seconds")
        with _lock:
            _dirty.clear()
        jobs = {job.id: compute_job(job) for job in _load_jobs()}
        snap = Snapshot(built_monotonic=start, built_at=built_at, jobs=jobs)
        with _lock:
            _snapshot = snap
        elapsed = time.monotonic() - start
    if elapsed >= SLOW_BUILD_SECONDS or reason == "stale on read":
        breadcrumbs.warning(
            "⏱️ jobs snapshot rebuilt (%s): %d jobs in %.2fs", reason, len(jobs), elapsed,
        )
    else:
        logger.info("ℹ️ jobs snapshot rebuilt (%s): %d jobs in %.2fs", reason, len(jobs), elapsed)
    return snap


def mark_dirty(job_id: Optional[str] = None) -> None:
    """Recompute ``job_id`` (every job when ``None``) on the next read.

    Called when this process changes a run's record or a job's stats cache.
    """
    with _lock:
        ids = [job_id] if job_id is not None else list(_snapshot.jobs if _snapshot else [])
        for jid in ids:
            _dirty[jid] = next(_marks)


def current(jobs: Iterable[Any]) -> Snapshot:
    """The snapshot to answer a request from, never older than MAX_AGE_SECONDS.

    ``jobs`` is the registry the request is decorating. A job missing from
    the snapshot (just created) or marked dirty is recomputed before
    answering, and merged into the live snapshot for the next reader.
    Blocking — callers are already off the event loop.
    """
    global _last_demand, _snapshot
    jobs = list(jobs)
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
        marks = dict(_dirty)
    stale = [job for job in jobs if job.id in marks or job.id not in snap.jobs]
    if not stale:
        return snap
    updates = {job.id: compute_job(job) for job in stale}
    with _lock:
        live = _snapshot
        if live is not None and live.built_monotonic == snap.built_monotonic:
            # Merge into the live mapping, not the one read above: another
            # reader may have merged its own recompute meanwhile.
            snap = Snapshot(live.built_monotonic, live.built_at, {**live.jobs, **updates})
            _snapshot = snap
            # Clear only marks this recompute covered; one set since stays.
            for jid, seq in marks.items():
                if jid in updates and _dirty.get(jid) == seq:
                    del _dirty[jid]
        else:
            # A rebuild swapped in meanwhile. Answer this read with what it
            # computed; the marks it did not clear stay for the next read.
            snap = Snapshot(snap.built_monotonic, snap.built_at, {**snap.jobs, **updates})
    return snap


def age_seconds(snap: Snapshot) -> float:
    return round(max(0.0, time.monotonic() - snap.built_monotonic), 1)


def describe(snap: Snapshot) -> Dict[str, Any]:
    """The staleness fields both handlers attach to their response."""
    return {"built_at": snap.built_at, "age_seconds": age_seconds(snap)}


def warm_caches() -> None:
    """Refresh the schtasks-backed caches the jobs route reads per request.

    Next-run times and missed-fire coverage keep their own TTL caches; left
    alone, a poll would pay the ``schtasks`` query or the coverage scan every
    time one expired. Run by the tick outside the build lock, so a slow
    ``schtasks`` delays only this, never a snapshot read.
    """
    from src import jobs_coverage, jobs_schtasks

    jobs_schtasks.warm_bulk_records(WARM_AHEAD_SECONDS)
    jobs_coverage.coverage_map(
        max_age=max(0.0, jobs_coverage._COVERAGE_TTL_SECONDS - WARM_AHEAD_SECONDS)
    )


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
            # A read that just rebuilt inline needs no second walk.
            await asyncio.to_thread(rebuild, "tick", unless_younger_than=TICK_SECONDS / 2)
        except Exception:  # noqa: BLE001 — a failed tick must not end the loop
            breadcrumbs.exception("❌ jobs snapshot tick failed; reads will rebuild inline")
        try:
            await asyncio.to_thread(warm_caches)
        except Exception:  # noqa: BLE001
            breadcrumbs.exception("❌ jobs cache warm-up failed; polls pay the refresh")


def latest_description() -> Optional[Dict[str, Any]]:
    """:func:`describe` of the snapshot last built, or ``None`` before any."""
    with _lock:
        snap = _snapshot
    return describe(snap) if snap is not None else None


def reset() -> None:
    """Drop all state. Tests only."""
    global _snapshot, _last_demand
    with _lock:
        _snapshot = None
        _dirty.clear()
        _last_demand = 0.0
