"""Run-duration percentiles, health, and stuck-run detection for the Jobs tab.

Split out of :mod:`src.jobs` (issue #315) — run-history file storage lives
in :mod:`src.jobs_history`, schtasks sync + spawn helpers in
:mod:`src.jobs_schtasks`, and the mutex queue in :mod:`src.jobs_queue`.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timedelta
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from src.jobs_config import Job
from src.jobs_history import latest_run, list_runs

# Process-local TTL cache. Reset per job by `invalidate_stats_cache` once a
# run finalises so the row updates promptly without waiting out the TTL.
_STATS_TTL_SECONDS = 30.0
_stats_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_stats_lock = Lock()


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def cooldown_check(
    job: Job, *, now: Optional[datetime] = None
) -> Optional[Tuple[int, int, str]]:
    """Return cooldown state for ``job``, or ``None`` when allowed to run.

    Returns ``(remaining_seconds, cooldown_seconds, anchor_run_id)`` when
    ``job`` is inside its cooldown window. ``remaining_seconds`` is the
    ceiling — i.e. always ``>= 1`` when returned — suitable for a
    ``Retry-After`` header.

    The anchor is the most recent **non-skipped** run. Measuring against
    skipped records too would turn a fixed cooldown into a sliding
    debounce: every rejected mash-fire would push the next allowed fire
    further away. So skipped records are explicitly ignored when picking
    the anchor.

    Returns ``None`` when:
      * the job has no cooldown configured (``None`` or ``0``),
      * the job has never produced a non-skipped run,
      * the anchor's ``started_at`` is missing or unparseable, or
      * the most recent non-skipped run started long enough ago.
    """
    cooldown = job.cooldown_seconds
    if not cooldown:
        return None
    anchor: Optional[Dict[str, Any]] = None
    for run in list_runs(job.id):
        # skipped (cooldown no-ops) and dry-run records (issue #69) are
        # not real fires — counting them as the anchor would let a dry
        # verification reset the cooldown window.
        if run.get("status") in (
            "skipped",
            "dry_run_success",
            "dry_run_failed",
        ):
            continue
        anchor = run
        break
    if anchor is None:
        return None
    started = _parse_iso(anchor.get("started_at"))
    if started is None:
        return None
    reference = now or datetime.now()
    elapsed = (reference - started).total_seconds()
    remaining = cooldown - elapsed
    if remaining <= 0:
        return None
    return int(math.ceil(remaining)), cooldown, str(anchor.get("run_id") or "")


def _duration_for(record: Dict[str, Any]) -> Optional[float]:
    """Pick up persisted ``duration_seconds`` or derive from started/finished."""
    persisted = record.get("duration_seconds")
    if isinstance(persisted, (int, float)) and persisted >= 0:
        return float(persisted)
    started = _parse_iso(record.get("started_at"))
    finished = _parse_iso(record.get("finished_at"))
    if started and finished and finished >= started:
        return (finished - started).total_seconds()
    return None


def _percentile(values: List[float], pct: float) -> Optional[float]:
    """Plain nearest-rank percentile — no SciPy. ``pct`` is in [0, 1]."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    idx = max(0, min(len(s) - 1, int(round(pct * (len(s) - 1)))))
    return s[idx]


def _compute_stats(job_id: str, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Read-only computation of ``run_stats`` — see :func:`run_stats`."""
    runs = list_runs(job_id)  # newest first
    now = now or datetime.now()
    completed_durations: List[float] = []
    success_recent = 0
    failed_recent = 0
    cutoff = now - timedelta(days=30)
    for r in runs:
        status = r.get("status")
        started = _parse_iso(r.get("started_at"))
        if status in {"success", "failed"}:
            d = _duration_for(r)
            if d is not None:
                completed_durations.append(d)
            if started and started >= cutoff:
                if status == "success":
                    success_recent += 1
                else:
                    failed_recent += 1
    # last7 oldest-left so the sparkline reads left→right chronologically.
    last7 = [
        {"status": r.get("status"), "run_id": r.get("run_id")}
        for r in list(reversed(runs[:7]))
    ]
    p50 = _percentile(completed_durations, 0.5)
    p95 = _percentile(completed_durations, 0.95)
    total_recent = success_recent + failed_recent
    return {
        "p50": p50,
        "p95": p95,
        "success_rate_30d": (success_recent / total_recent) if total_recent else None,
        "completed_count": len(completed_durations),
        "last7": last7,
    }


def run_stats(job_id: str, *, fresh: bool = False) -> Dict[str, Any]:
    """Return aggregated stats for ``job_id`` (process-local 30 s cache).

    Shape::

        {
          "p50": Optional[float],          # seconds, completed runs only
          "p95": Optional[float],
          "success_rate_30d": Optional[float],  # None when zero recent runs
          "completed_count": int,
          "last7": [{"status": str, "run_id": str}, ...]  # oldest-left
        }

    ``fresh=True`` skips the cache — used by the stuck-run check, which
    pays the cost rarely (only when the latest run is still running).
    """
    now = time.monotonic()
    if not fresh:
        with _stats_lock:
            hit = _stats_cache.get(job_id)
            if hit is not None and now - hit[0] < _STATS_TTL_SECONDS:
                return hit[1]
    stats = _compute_stats(job_id)
    with _stats_lock:
        _stats_cache[job_id] = (now, stats)
    return stats


def invalidate_stats_cache(job_id: Optional[str] = None) -> None:
    """Drop one job's cached stats (or all of them when ``job_id`` is None).

    Called after a run finalises so the row updates promptly without
    waiting out the 30 s TTL.
    """
    with _stats_lock:
        if job_id is None:
            _stats_cache.clear()
        else:
            _stats_cache.pop(job_id, None)


# How many *completed* runs a job needs before its derived duration
# threshold is trusted to auto-kill (issue #695). The same threshold has
# always been safe to *warn* on with zero history — a false ⚠️ costs a
# glance — but killing a healthy run is not recoverable, so the executor's
# watchdog refuses to derive a ceiling until the history can carry it. A
# job with thinner history than this gets no derived ceiling at all
# (``None`` — "can't tell", never "300 s"); set ``max_runtime_seconds``
# explicitly to give it one.
WATCHDOG_MIN_COMPLETED_RUNS = 5


def _threshold_from_stats(
    stats: Dict[str, Any], p95_factor: float, floor_seconds: float
) -> float:
    """``max(p95 × factor, floor_seconds)`` off an already-computed stats dict."""
    p95 = stats.get("p95") or 0.0
    return max(p95 * p95_factor, floor_seconds)


def stuck_threshold_seconds(
    job_id: str, *, p95_factor: float = 3.0, floor_seconds: float = 300.0
) -> float:
    """Seconds a run may sit ``running`` before it looks stuck.

    The one place the "too long for *this* job" heuristic is defined.
    :func:`is_stuck` (the surface-only ⚠️ badge) and the executor's
    last-resort watchdog (issue #695) both read it, so the warning line
    and the auto-kill ceiling can never drift apart.
    """
    return _threshold_from_stats(
        run_stats(job_id, fresh=True), p95_factor, floor_seconds
    )


def derived_runtime_ceiling_seconds(
    job_id: str,
    *,
    p95_factor: float = 3.0,
    floor_seconds: float = 300.0,
    min_completed: int = WATCHDOG_MIN_COMPLETED_RUNS,
) -> Optional[float]:
    """The watchdog's runtime ceiling for a job that sets none explicitly.

    Same threshold as :func:`stuck_threshold_seconds`, but ``None`` when
    the job has fewer than ``min_completed`` completed runs on record —
    a first-ever run, or a job whose history was just pruned, has no
    evidence for what "too long" means, and the floor would kill it at
    five minutes. Unknown is returned as unknown, not as a threshold.
    """
    stats = run_stats(job_id, fresh=True)
    if int(stats.get("completed_count") or 0) < min_completed:
        return None
    return _threshold_from_stats(stats, p95_factor, floor_seconds)


def is_stuck(
    job_id: str, *, p95_factor: float = 3.0, floor_seconds: float = 300.0
) -> bool:
    """``True`` when the latest run is ``running`` past a sane threshold.

    Threshold = :func:`stuck_threshold_seconds`. Surface-only — the UI
    shows ⚠️ and exposes a manual kill button. The *auto*-kill lives in
    the executor's watchdog (issue #695), which derives its ceiling from
    the same helper but demands a minimum sample first.
    """
    latest = latest_run(job_id)
    if not latest or latest.get("status") != "running":
        return False
    started = _parse_iso(latest.get("started_at"))
    if not started:
        return False
    threshold = stuck_threshold_seconds(
        job_id, p95_factor=p95_factor, floor_seconds=floor_seconds
    )
    elapsed = (datetime.now() - started).total_seconds()
    return elapsed > threshold


def consecutive_failed_runs(job_id: str) -> int:
    """Count the contiguous ``failed`` runs at the top of the history.

    Stops at the first non-failed (success / running / pending / unknown).
    Used by the notification streak gate.
    """
    n = 0
    for r in list_runs(job_id):
        if r.get("status") != "failed":
            break
        n += 1
    return n
