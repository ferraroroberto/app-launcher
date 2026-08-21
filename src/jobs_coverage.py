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
* **Principal** (``session_less`` jobs only, issue #757) — an entry that
  exists, is enabled, and is *still* registered ``Interactive only`` passes
  both halves above and silently does nothing whenever the machine sits
  logged out. Read from the same cached bulk query as the structural half.

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

**Each problem class is announced in its own words** (issue #778, table in
:data:`PROBLEM_ANNOUNCEMENTS`). All four used to share one hardcoded title —
"scheduled run never fired" — which reads as an observed failure and is only
true of ``missed_fire``; the other three are structural risks about whether
the entry *could* fire. A ``session_less`` backup that had run on time for six
straight days was announced daily as never having fired, which is precisely
how a reader learns to dismiss the channel. Severity and re-ping cadence
follow the class too: a hard failure stays ``error`` at
:data:`COVERAGE_ALERT_REPEAT_SECONDS`, while a conditional risk that needs a
human at an elevated shell drops to ``warning`` at
:data:`COVERAGE_ALERT_REPEAT_SECONDS_LOW` — quieter, never silent.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from src import jobs_history
from src._json_io import atomic_write_json
from src.jobs_config import Job, load_jobs
from src.jobs_schtasks import (
    FREQUENT_SCHEDULE_TYPES,
    registered_task_principals,
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
# Re-ping interval for a *conditional* structural risk — one where the job is
# still firing today and the remediation needs a human at an elevated shell
# (issue #778). The condition keeps being reported; it just doesn't earn a
# daily high-priority ping, which is how a channel gets muted.
COVERAGE_ALERT_REPEAT_SECONDS_LOW = 7 * 24 * 3600.0

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
#: A ``session_less`` job (issue #757) whose Task Scheduler entry is still
#: registered ``Interactive only`` — it exists and looks healthy to every
#: other check, and silently does nothing whenever the box sits logged out.
#: Either it was never hand-registered from an elevated shell, or something
#: re-registered it with Windows' default principal.
PROBLEM_PRINCIPAL_INTERACTIVE = "principal_interactive"


class Announcement(NamedTuple):
    """How one problem class is announced on the phone (issue #778).

    ``title`` is a format string taking ``{name}`` (the job's display name).
    ``severity`` maps to a Pushover priority in
    :class:`src.notifications.PushoverNotifier`; ``repeat_seconds`` is how
    long a standing problem of this class waits before re-pinging.
    """

    title: str
    severity: str
    repeat_seconds: float


#: Per-class alert vocabulary, ordered **most root-causal first**.
#:
#: Every class used to borrow one hardcoded title — "scheduled run never
#: fired" — which is a plain lie for three of the four (issue #778): only
#: :data:`PROBLEM_MISSED_FIRE` is a fact about a run that didn't happen. The
#: other three are structural risks about whether the entry *could* fire, and
#: ``principal_interactive`` in particular describes a *future* condition (the
#: box sitting logged out) on a job that is firing on time today. Announcing
#: a healthy backup as "never fired" trains the reader to dismiss the channel,
#: and then a real missed fire lands in a channel nobody reads.
#:
#: A title must therefore state what was actually *observed*, and no class may
#: borrow another's vocabulary.
PROBLEM_ANNOUNCEMENTS: Tuple[Tuple[str, Announcement], ...] = (
    (
        PROBLEM_TASK_MISSING,
        Announcement(
            "🕳️ {name} — Task Scheduler entry missing",
            "error",
            COVERAGE_ALERT_REPEAT_SECONDS,
        ),
    ),
    (
        PROBLEM_TASK_DISABLED,
        Announcement(
            "🚫 {name} — Task Scheduler entry disabled",
            "error",
            COVERAGE_ALERT_REPEAT_SECONDS,
        ),
    ),
    (
        PROBLEM_MISSED_FIRE,
        Announcement(
            "🕳️ {name} — scheduled run never fired",
            "error",
            COVERAGE_ALERT_REPEAT_SECONDS,
        ),
    ),
    (
        PROBLEM_PRINCIPAL_INTERACTIVE,
        Announcement(
            "🔒 {name} — will not fire while logged out",
            "warning",
            COVERAGE_ALERT_REPEAT_SECONDS_LOW,
        ),
    ),
)

#: Used when a ``problem`` verdict carries a class this table doesn't know —
#: a new constant added without its announcement. Deliberately vague rather
#: than wrong: it says a coverage problem exists and lets the body speak.
FALLBACK_ANNOUNCEMENT = Announcement(
    "🕳️ {name} — schedule coverage problem",
    "error",
    COVERAGE_ALERT_REPEAT_SECONDS,
)

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
    interactive_tasks: Optional[List[str]] = None,
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
        "interactive_tasks": interactive_tasks or [],
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
    principals: Optional[Dict[str, Optional[bool]]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Coverage verdict for one job.

    ``task_states`` is :func:`src.jobs_schtasks.registered_task_states`'
    output — ``None`` when the query failed, in which case the structural
    half reports ``unknown`` instead of inventing missing tasks. A per-task
    ``None`` value means "registered, enabled-state unreadable": not a
    problem, because the task demonstrably exists.

    ``principals`` is :func:`src.jobs_schtasks.registered_task_principals`'
    output, read from the same cached snapshot. It is only consulted for a
    ``session_less`` job (issue #757), where "the entry exists and is
    enabled" is *not* sufficient: an entry left on the default
    ``InteractiveToken`` principal passes every other check here and still
    does nothing whenever the machine sits logged out. Same tri-state rule
    as everything else — an unreadable principal yields ``unknown``, never a
    confident pass.

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

    # Principal half (issue #757) — only meaningful for a job that declared
    # it must run session-less. A task already reported missing is skipped:
    # the structural half owns that, and "missing" plus "wrong principal"
    # for one entry is one problem told twice.
    interactive: List[str] = []
    principal_unknown = False
    if job.session_less:
        if principals is None:
            principal_unknown = True
        else:
            for name in task_names_for(job):
                capable = principals.get(name, _MISSING)
                if capable is _MISSING:
                    continue
                if capable is False:
                    interactive.append(name)
                elif capable is None:
                    principal_unknown = True

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
    if interactive:
        problems.append(PROBLEM_PRINCIPAL_INTERACTIVE)
        bits.append(
            f"{len(interactive)} Task Scheduler entr"
            f"{'y is' if len(interactive) == 1 else 'ies are'} still "
            "'Interactive only' — will not fire while logged out; "
            "re-register from an elevated shell"
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
            interactive_tasks=interactive,
            missed=missed,
        )
    unresolved: List[str] = []
    if structural_unknown:
        unresolved.append("Task Scheduler query failed — coverage not established")
    if principal_unknown:
        unresolved.append(
            "Task Scheduler logon mode unreadable — session-less principal "
            "not established"
        )
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
    # Same cached snapshot as the line above — no second shell-out.
    principals = registered_task_principals()
    now = now or datetime.now()
    out: Dict[str, Dict[str, Any]] = {}
    for job in jobs:
        try:
            out[job.id] = coverage_for(
                job, task_states, principals=principals, now=now
            )
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


def announcement_for(problems: Any) -> Announcement:
    """How to announce a ``problem`` verdict carrying *problems* (issue #778).

    The **title and severity** come from the highest-precedence class present
    in :data:`PROBLEM_ANNOUNCEMENTS` — a missing Task Scheduler entry explains
    a missed fire, so it outranks it, and the alert body already spells out
    every detected class, so nothing is lost by the title naming one.

    The **repeat interval** is the shortest of every class present, not the
    title's. A job holding both a conditional risk and a hard failure must
    keep the hard failure's daily cadence; today the low-cadence class sorts
    last so this is also what precedence would give, but that is incidental
    and a future reordering must not quietly stretch an outage's re-ping.

    An empty or unrecognised list yields :data:`FALLBACK_ANNOUNCEMENT` — a
    ``problem`` state always deserves *some* announcement, and a vague one
    beats borrowing another class's wording.
    """
    present = set(problems or ())
    chosen: Optional[Announcement] = None
    repeat: Optional[float] = None
    for problem, announcement in PROBLEM_ANNOUNCEMENTS:
        if problem not in present:
            continue
        if chosen is None:
            chosen = announcement
        repeat = (
            announcement.repeat_seconds
            if repeat is None
            else min(repeat, announcement.repeat_seconds)
        )
    if chosen is None:
        return FALLBACK_ANNOUNCEMENT
    return chosen._replace(repeat_seconds=repeat)


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
            announcement = announcement_for(result.get("problems"))
            signature = _signature(result)
            prior = state.get(job.id) or {}
            last_epoch = prior.get("last_alert_epoch")
            recent = (
                prior.get("signature") == signature
                and isinstance(last_epoch, (int, float))
                and (wall - last_epoch) < announcement.repeat_seconds
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

            title = announcement.title.format(name=job.name)
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
                    push.notify(title, body, severity=announcement.severity)
            if job.alert_on_failure:
                tg = telegram_notifier or build_telegram_notifier_from_config(cfg)
                if not isinstance(tg, NoopNotifier):
                    tg.notify(title, body, severity=announcement.severity)
            alerted.append(job.id)

        if dirty:
            _write_alert_state(state)
    except Exception as exc:  # noqa: BLE001 — background tick must not die
        logger.warning(f"⚠️  coverage check raised: {exc}")
    return alerted
