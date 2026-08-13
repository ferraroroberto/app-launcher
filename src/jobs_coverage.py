"""Missed-fire coverage for scheduled jobs (issue #697).

``alert_on_failure`` catches a run that *fails*; ``src.jobs_stats.is_stuck``
catches a run that never *ends*. Nothing caught a run that never **starts** —
a job whose Task Scheduler entry is missing, mangled, disabled, or was never
created simply doesn't fire, and the absence was invisible: the row kept
showing its old stats and no alert existed for "expected a run, saw none".

Two independent halves, both answered from data the Jobs tab already reads:

* **Structural** — every non-paused, scheduled job must have a matching,
  enabled ``\\AppLauncher\\<id>`` Task Scheduler entry. This is the cheap
  half that would have caught both real incidents (``config-map`` and
  ``sota-watch`` shipped launchers + "runs weekly unattended" docs with no
  registered task at all, for weeks), and it fires the moment the entry
  disappears — no waiting for the missed slot itself. Backed by
  :func:`src.jobs_schtasks.registered_task_states`, i.e. the *same* 30 s
  cached bulk ``schtasks /Query`` the "next run" column already pays for:
  one batched query per cycle, never an N+1 shell-out storm.
* **Behavioural** — expand the schedule across a recent window and check
  each expected fire against the on-disk run history.

Never-flag rules (the acceptance criterion is "no false positives across a
normal week"), all enforced in :func:`behavioural_coverage` /
:func:`coverage_for`:

* Paused jobs and ``schedule: none`` jobs are **exempt** — no state at all.
* ``minutes``/``hourly`` jobs skip the behavioural half: their cadence is too
  dense to enumerate (:data:`~src.jobs_schtasks.FREQUENT_SCHEDULE_TYPES`,
  same reason the agenda summarises them). The structural half still covers
  them, which is what actually detects a deleted entry.
* A slot only counts as missed once it is ``MISSED_FIRE_GRACE_SECONDS`` past
  — Task Scheduler starts late, and a run that started is a run that fired.
* **Any** run record near the slot counts as a fire, whatever its status —
  including ``skipped`` (a cooldown no-op *did* fire, it just declined to do
  work) and a manual run that happened to cover the slot.
* The window never reaches back past the job's ``added_at``, nor past the
  oldest retained run when history is at its :data:`MAX_RUNS_PER_JOB` cap —
  a pruned record is not evidence of a missed fire.
* A failed ``schtasks`` query yields ``unknown``, never "missing". An
  unestablished fact gets its own state and is never folded into the
  passing *or* the failing one.
* A job with **no run records at all** yields ``unknown``, never
  "missed fire" (issue #737). :func:`src.jobs_history.list_runs` returns
  ``[]`` both for a job that genuinely never ran and for a checkout with no
  run history to read, and the behavioural half cannot tell those apart —
  so it says so. Run history counts as evidence only where some exists;
  a job with *some* history that skipped a slot is still a ``problem``,
  which is the case #697 exists for. The same rule as the ``schtasks`` one
  above, applied to the other half. This is not hypothetical: a stray webapp
  booted out of a git worktree (tracked files only, hence an empty
  ``webapp/jobs/``) while holding a full real ``jobs.json`` flagged seven
  jobs and pushed three false "never fired" alerts to the phone on
  2026-08-10.

Alerting reuses the exact channels the failure path uses
(:func:`src.notifications.notify_failure`): global Pushover
gated by ``WebappConfig.notify_on_failure``, per-job Telegram gated by
``Job.alert_on_failure``. De-duplicated through a small on-disk state file so
a standing problem pings once, not once per check cycle.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from src import jobs_history
from src._json_io import atomic_write_json
from src.jobs_config import Job, load_jobs
from src.jobs_schtasks import (
    FREQUENT_SCHEDULE_TYPES,
    registered_task_states,
    task_names_for,
    upcoming_fires,
)

logger = logging.getLogger(__name__)

# How far back the behavioural half looks. Three days covers a daily job's
# last few slots and a weekly job's slot without dragging in history the
# 20-run / 30-day retention may already have pruned.
COVERAGE_WINDOW_DAYS = 3
# A slot is only "missed" once this much wall-clock has passed without a run
# record — Task Scheduler routinely starts a task tens of seconds late, and a
# machine waking from sleep can be minutes late.
MISSED_FIRE_GRACE_SECONDS = 900.0
# A run may legitimately be stamped slightly *before* its nominal slot
# (schtasks fires, the executor stamps started_at, clocks round differently).
MISSED_FIRE_EARLY_TOLERANCE_SECONDS = 90.0
# Only the N most recent missed slots are carried in the payload — the UI
# shows a count and the newest few, not an unbounded list.
MAX_REPORTED_MISSED_FIRES = 5

# Process-local TTL cache, mirroring src.jobs_stats' 30 s stats cache. The
# check itself is cheap (one cached schtasks read + the run-history walk
# `/api/jobs` already does), but it is called once per job per poll.
_COVERAGE_TTL_SECONDS = 60.0
_coverage_cache: Optional[tuple] = None
_coverage_lock = Lock()

# Re-ping a still-broken job at most this often, so a job left broken over a
# weekend doesn't fire an alert every check cycle.
COVERAGE_ALERT_REPEAT_SECONDS = 24 * 3600.0

#: Where the alert de-duplication state lives. Sits beside the per-job run
#: directories rather than in config — it is derived, disposable state, and
#: losing it costs exactly one duplicate ping.
COVERAGE_ALERTS_FILENAME = "coverage-alerts.json"

STATE_OK = "ok"
STATE_PROBLEM = "problem"
STATE_UNKNOWN = "unknown"
STATE_EXEMPT = "exempt"

PROBLEM_TASK_MISSING = "task_missing"
PROBLEM_TASK_DISABLED = "task_disabled"
PROBLEM_MISSED_FIRE = "missed_fire"

_MISSING = object()


def coverage_alerts_path() -> Path:
    """Where the alert de-duplication state file lives.

    Resolved through :data:`src.jobs_history.JOBS_RUNS_DIR` at call time (not
    import time) so a test monkeypatching that directory redirects this too —
    same module-attribute access :mod:`src.jobs_index` uses for its own
    sibling file in that directory.
    """
    return jobs_history.JOBS_RUNS_DIR / COVERAGE_ALERTS_FILENAME


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _result(
    state: str,
    *,
    detail: str,
    problems: Optional[List[str]] = None,
    missing_tasks: Optional[List[str]] = None,
    disabled_tasks: Optional[List[str]] = None,
    missed: Optional[List[datetime]] = None,
) -> Dict[str, Any]:
    """The JSON-serialisable coverage payload attached to a job row."""
    missed = missed or []
    return {
        "state": state,
        "detail": detail,
        "problems": problems or [],
        "missing_tasks": missing_tasks or [],
        "disabled_tasks": disabled_tasks or [],
        "missed_count": len(missed),
        "missed_fires": [
            f.isoformat(timespec="minutes")
            for f in missed[-MAX_REPORTED_MISSED_FIRES:]
        ],
    }


def _no_history_detail() -> str:
    """The honest "cannot establish" line for a job with zero run records.

    Two materially different diagnoses, so two messages (issue #737):

    * The run-history store holds no per-job history at all — either the
      directory is absent or nothing has ever been recorded under it. That is
      the git-worktree shape that caused the false alerts, and naming the
      resolved path is the breadcrumb that identifies the stray checkout on
      sight, since :data:`src.jobs_history.JOBS_RUNS_DIR` is derived from the
      module's own ``PROJECT_ROOT``.
    * The store is live for other jobs but has nothing for this one.

    Either way the verdict is ``unknown`` — this only picks the wording.
    """
    store = jobs_history.JOBS_RUNS_DIR
    try:
        populated = any(child.is_dir() for child in store.iterdir())
    except OSError:
        populated = False
    if populated:
        return (
            "no run history for this job — a missed fire and a missing "
            "history are indistinguishable"
        )
    return f"no run history at all under {store} — coverage not established"


def behavioural_coverage(
    job: Job,
    *,
    now: Optional[datetime] = None,
    window_days: int = COVERAGE_WINDOW_DAYS,
    grace_seconds: float = MISSED_FIRE_GRACE_SECONDS,
) -> Tuple[List[datetime], Optional[str]]:
    """The behavioural half's verdict: ``(missed fires, unknown reason)``.

    Exactly one of the two is ever meaningful. ``(missed, None)`` is an
    established answer — the run history was usable and these slots are
    genuinely uncovered (``[]`` meaning "all covered", or nothing to check).
    ``([], reason)`` means the question could not be answered at all, and the
    caller must report ``unknown`` rather than either passing or failing the
    job (issue #737; see the module docstring's never-flag rules).

    Missed slots are oldest first. Both lists are empty for the dense
    :data:`~src.jobs_schtasks.FREQUENT_SCHEDULE_TYPES`, for a schedule with
    no computable fires, and whenever the usable window collapses — the
    window is clamped by ``added_at`` and by the oldest retained run once
    history is at its :data:`~src.jobs_history.MAX_RUNS_PER_JOB` cap. Those
    are not ``unknown``: nothing was expected, so nothing is unestablished.
    """
    if job.schedule.type in FREQUENT_SCHEDULE_TYPES:
        return [], None
    now = now or datetime.now()
    deadline = now - timedelta(seconds=grace_seconds)
    window_start = now - timedelta(days=window_days)

    added = _parse_iso(job.added_at)
    if added is not None and added > window_start:
        window_start = added

    runs = jobs_history.list_runs(job.id)  # newest first
    starts: List[datetime] = []
    for record in runs:
        started = _parse_iso(record.get("started_at"))
        if started is not None:
            starts.append(started)
    # History is capped, so an absent record older than the oldest retained
    # run proves nothing — it may simply have been pruned.
    if len(runs) >= jobs_history.MAX_RUNS_PER_JOB and starts:
        oldest = min(starts)
        if oldest > window_start:
            window_start = oldest

    if deadline <= window_start:
        return [], None

    fires = upcoming_fires(job.schedule, start=window_start, end=deadline)
    if not fires:
        return [], None
    if not runs:
        # Slots elapsed and not one run record exists to check them against.
        # "Never fired" and "no history here to read" look identical from
        # here, so neither is claimed.
        detail = _no_history_detail()
        logger.debug(f"coverage for {job.id} not established: {detail}")
        return [], detail

    starts.sort()
    early = timedelta(seconds=MISSED_FIRE_EARLY_TOLERANCE_SECONDS)
    late = timedelta(seconds=grace_seconds)
    missed: List[datetime] = []
    for fire in fires:
        covered = any(fire - early <= s <= fire + late for s in starts)
        if not covered:
            missed.append(fire)
    return missed, None


def missed_fires(
    job: Job,
    *,
    now: Optional[datetime] = None,
    window_days: int = COVERAGE_WINDOW_DAYS,
    grace_seconds: float = MISSED_FIRE_GRACE_SECONDS,
) -> List[datetime]:
    """Expected fires of ``job`` in the recent window with no run record.

    The list half of :func:`behavioural_coverage`. Slots are only reported
    where the run history could actually back them up; a job with no usable
    history yields ``[]`` here and its ``unknown`` reason there.
    """
    return behavioural_coverage(
        job, now=now, window_days=window_days, grace_seconds=grace_seconds
    )[0]


def coverage_for(
    job: Job,
    task_states: Optional[Dict[str, Optional[bool]]],
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Coverage verdict for one job.

    ``task_states`` is :func:`src.jobs_schtasks.registered_task_states`'
    output — ``None`` when the query failed, in which case the structural
    half reports ``unknown`` instead of inventing missing tasks. A per-task
    ``None`` value means "registered, enabled-state unreadable": not a
    problem, because the task demonstrably exists.

    Either half may come back unestablished, and the precedence is: a real
    problem first (it rests on its own evidence and outranks the other
    half's silence), then ``unknown`` if *either* half could not establish
    its fact, then ``ok``. So a job whose Task Scheduler entry is missing is
    still a ``problem`` even with no run history to read (issue #737), which
    is the shape of both incidents #697 was built for.
    """
    if job.is_paused or job.schedule.type == "none":
        return _result(STATE_EXEMPT, detail="no active schedule")

    missing: List[str] = []
    disabled: List[str] = []
    structural_unknown = task_states is None
    if task_states is not None:
        for name in task_names_for(job):
            enabled = task_states.get(name, _MISSING)
            if enabled is _MISSING:
                missing.append(name)
            elif enabled is False:
                disabled.append(name)

    missed, behavioural_unknown = behavioural_coverage(job, now=now)

    problems: List[str] = []
    bits: List[str] = []
    if missing:
        problems.append(PROBLEM_TASK_MISSING)
        bits.append(
            f"{len(missing)} Task Scheduler entr"
            f"{'y' if len(missing) == 1 else 'ies'} missing"
        )
    if disabled:
        problems.append(PROBLEM_TASK_DISABLED)
        bits.append(
            f"{len(disabled)} Task Scheduler entr"
            f"{'y' if len(disabled) == 1 else 'ies'} disabled"
        )
    if missed:
        problems.append(PROBLEM_MISSED_FIRE)
        bits.append(
            f"{len(missed)} scheduled fire{'' if len(missed) == 1 else 's'} "
            f"produced no run (last {missed[-1].isoformat(timespec='minutes')})"
        )

    if problems:
        return _result(
            STATE_PROBLEM,
            detail="; ".join(bits),
            problems=problems,
            missing_tasks=missing,
            disabled_tasks=disabled,
            missed=missed,
        )
    unresolved: List[str] = []
    if structural_unknown:
        unresolved.append("Task Scheduler query failed — coverage not established")
    if behavioural_unknown:
        unresolved.append(behavioural_unknown)
    if unresolved:
        return _result(STATE_UNKNOWN, detail="; ".join(unresolved))
    return _result(STATE_OK, detail="schedule registered and firing")


def scan_coverage(
    jobs: Optional[List[Job]] = None, *, now: Optional[datetime] = None
) -> Dict[str, Dict[str, Any]]:
    """Coverage verdicts for every job, keyed by job id.

    One batched ``schtasks`` read for the whole scan (the acceptance
    criterion's "no schtasks shell-out storm"), then pure per-job work.
    """
    jobs = load_jobs().jobs if jobs is None else jobs
    task_states = registered_task_states()
    now = now or datetime.now()
    out: Dict[str, Dict[str, Any]] = {}
    for job in jobs:
        try:
            out[job.id] = coverage_for(job, task_states, now=now)
        except OSError as exc:
            logger.debug(f"coverage scan skipped {job.id}: {exc}")
            out[job.id] = _result(
                STATE_UNKNOWN, detail="run history unreadable"
            )
    return out


def coverage_map(*, fresh: bool = False) -> Dict[str, Dict[str, Any]]:
    """:func:`scan_coverage` behind a process-local TTL cache.

    ``/api/jobs`` decorates every row from one cached scan per poll rather
    than re-deriving per job.
    """
    global _coverage_cache
    monotonic = time.monotonic()
    if not fresh:
        with _coverage_lock:
            if _coverage_cache is not None:
                ts, snapshot = _coverage_cache
                if monotonic - ts < _COVERAGE_TTL_SECONDS:
                    return snapshot
    snapshot = scan_coverage()
    with _coverage_lock:
        _coverage_cache = (monotonic, snapshot)
    return snapshot


def coverage_for_job(job_id: str) -> Dict[str, Any]:
    """One job's cached coverage verdict.

    ``unknown`` when the job isn't in the current snapshot (added since the
    last scan) — never a fabricated ``ok``.
    """
    snapshot = coverage_map()
    return snapshot.get(
        job_id, _result(STATE_UNKNOWN, detail="not yet scanned")
    )


def invalidate_coverage_cache() -> None:
    """Drop the cached scan — called whenever schtasks state is rewritten."""
    global _coverage_cache
    with _coverage_lock:
        _coverage_cache = None


# ------------------------------------------------------------- alerting


def _read_alert_state() -> Dict[str, Any]:
    path = coverage_alerts_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_alert_state(state: Dict[str, Any]) -> None:
    path = coverage_alerts_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, state)
    except OSError as exc:
        logger.warning(f"⚠️  coverage alert state not persisted: {exc}")


def _signature(result: Dict[str, Any]) -> str:
    """Stable identity of a problem, so a *changed* problem re-alerts."""
    return "|".join(
        [
            ",".join(result.get("problems", [])),
            ",".join(sorted(result.get("missing_tasks", []))),
            ",".join(sorted(result.get("disabled_tasks", []))),
            str(result.get("missed_count", 0)),
        ]
    )


def check_and_alert(
    cfg: Any,
    *,
    jobs: Optional[List[Job]] = None,
    now: Optional[datetime] = None,
    notifier: Optional[Any] = None,
    telegram_notifier: Optional[Any] = None,
) -> List[str]:
    """Run a fresh scan and push alerts for newly-broken coverage.

    Returns the job ids alerted on this cycle. Same two channels, same
    gates as the failure path (``notify_on_failure`` for Pushover,
    ``Job.alert_on_failure`` for Telegram) — a coverage problem is a job
    problem, not a new notification surface.

    De-duplicated on disk: a job re-alerts only when its problem
    *signature* changes or :data:`COVERAGE_ALERT_REPEAT_SECONDS` have
    passed. A job whose coverage recovers is dropped from the state, so the
    next break alerts immediately.

    Never raises — this runs from a background tick and from the executor's
    tail; a broken notification path must not take either down.
    """
    # Imported here rather than at module scope: src.notifications pulls in
    # requests + the LLM client, and this module is imported by the webapp's
    # hot /api/jobs path purely for the badge.
    from src.notifications import (
        NoopNotifier,
        build_notifier_from_config,
        build_telegram_notifier_from_config,
    )

    alerted: List[str] = []
    try:
        jobs = load_jobs().jobs if jobs is None else jobs
        results = scan_coverage(jobs, now=now)
        invalidate_coverage_cache()
        state = _read_alert_state()
        stamp = (now or datetime.now()).isoformat(timespec="seconds")
        wall = time.time()
        dirty = False

        for job in jobs:
            result = results.get(job.id)
            if result is None:
                continue
            if result.get("state") != STATE_PROBLEM:
                if state.pop(job.id, None) is not None:
                    dirty = True
                continue
            signature = _signature(result)
            prior = state.get(job.id) or {}
            last_epoch = prior.get("last_alert_epoch")
            recent = (
                prior.get("signature") == signature
                and isinstance(last_epoch, (int, float))
                and (wall - last_epoch) < COVERAGE_ALERT_REPEAT_SECONDS
            )
            state[job.id] = {
                "signature": signature,
                "detail": result.get("detail", ""),
                "last_seen_at": stamp,
                "last_alert_epoch": last_epoch if recent else wall,
                "last_alert_at": prior.get("last_alert_at") if recent else stamp,
            }
            dirty = True
            if recent:
                continue

            title = f"🕳️ {job.name} — scheduled run never fired"
            body = (
                f"{result.get('detail', '')}\n"
                f"— job={job.id} schedule={job.schedule.chip()}"
            )
            logger.warning(
                f"🕳️ coverage problem for job {job.id}: {result.get('detail')}"
            )
            if getattr(cfg, "notify_on_failure", False):
                push = notifier or build_notifier_from_config(cfg)
                if not isinstance(push, NoopNotifier):
                    push.notify(title, body, severity="error")
            if job.alert_on_failure:
                tg = telegram_notifier or build_telegram_notifier_from_config(cfg)
                if not isinstance(tg, NoopNotifier):
                    tg.notify(title, body, severity="error")
            alerted.append(job.id)

        if dirty:
            _write_alert_state(state)
    except Exception as exc:  # noqa: BLE001 — background tick must not die
        logger.warning(f"⚠️  coverage check raised: {exc}")
    return alerted
