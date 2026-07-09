"""Windows Task Scheduler sync + spawn helpers for the Jobs tab.

Every :class:`~src.jobs_config.Job` with a non-``none`` schedule
materialises as one or more entries under the ``\\AppLauncher\\`` Task
Scheduler folder. ``daily_times`` jobs fan out into
``\\AppLauncher\\<id>-1``, ``…-2``, … so a single schedule with three
wake-ups becomes three Task Scheduler entries pointing at the same
executor.

Also owns the deterministic "next fire" arithmetic: the schtasks "Next Run
Time" string is a locale-formatted, lexically-sorted best-effort value —
fine to *display*, useless to *sort by* or turn into a countdown. The
schedule definition is a small deterministic set, so :func:`next_fire` /
:func:`upcoming_fires` compute the next wall-clock fire ourselves — this is
the field the UI sorts on and renders "in 3h" from. Both concerns answer
"when does this job run" (one via schtasks, one via pure computation), which
is why they share this module.

The single executor that ever runs a job is
:class:`~app.cli.commands.run_job_cmd.RunJobCommand`. Task Scheduler
calls it with ``pythonw launcher.py run-job <id>``; the webapp's
``POST /api/jobs/<id>/run`` route spawns the same command detached and
returns the new ``run_id`` immediately.

Split out of :mod:`src.jobs` (issue #315) — run-history file storage lives
in :mod:`src.jobs_history`, the mutex queue in :mod:`src.jobs_queue`, and
percentiles/health in :mod:`src.jobs_stats`.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.jobs_config import Job, Schedule

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

TASK_NAMESPACE = "AppLauncher"
TASK_FOLDER_PREFIX = f"\\{TASK_NAMESPACE}\\"

# Process-local TTL cache of the bulk schtasks "Next Run Time" query. The
# original Jobs-tab v1 shelled out to schtasks once per job, per /api/jobs
# poll (every 3 s while the tab was open) — N+1 fork+exec on Windows for
# what is effectively a static schedule. Reset by
# `invalidate_next_run_cache` whenever sync/delete writes change Task
# Scheduler state.
_NEXT_RUN_TTL_SECONDS = 30.0
_next_run_cache: Optional[Tuple[float, Dict[str, Optional[str]]]] = None
_next_run_lock = Lock()

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Defensive upper bound when blind-deleting daily_times variants without
# a query first. 24 covers every hour of the day with headroom.
_MAX_DAILY_TIMES_VARIANTS = 24


def resolve_venv_python(script_path: Path) -> Optional[Path]:
    """Walk up from ``script_path.parent`` looking for ``.venv\\Scripts\\python.exe``.

    Returns the resolved interpreter path, or ``None`` when no ancestor
    directory contains a ``.venv``. The walk stops at the filesystem root.
    Shared by the executor (``build_invocation``) and the save-time
    pre-flight (``src.jobs_preflight``).
    """
    try:
        cur = script_path.parent.resolve()
    except OSError:
        return None
    for parent in (cur, *cur.parents):
        candidate = parent / ".venv" / "Scripts" / "python.exe"
        if candidate.is_file():
            return candidate
    return None


# ----------------------------------------------------------- schtasks I/O


def _run_schtasks(argv: List[str]) -> subprocess.CompletedProcess:
    """Invoke ``schtasks.exe`` with ``argv``. Module-level so tests can mock it."""
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
        creationflags=_CREATE_NO_WINDOW,
    )


def _launcher_python(*, visible: bool = False) -> str:
    """The launcher's own venv interpreter, with PATH fallback.

    ``visible=True`` resolves ``python.exe`` (console subsystem) — used by
    jobs so the scheduled task fires with a console window in the
    logged-on session. ``visible=False`` (default) resolves the windowless
    ``pythonw.exe``.
    """
    name = "python.exe" if visible else "pythonw.exe"
    candidate = PROJECT_ROOT / ".venv" / "Scripts" / name
    if candidate.is_file():
        return str(candidate)
    return name


def _launcher_py() -> str:
    return str(PROJECT_ROOT / "launcher.py")


def task_run_command(job_id: str, *, visible: bool = False) -> str:
    """The /TR string Task Scheduler stores for ``job_id``.

    Quoted so paths-with-spaces survive Task Scheduler's tokenisation
    when it splits the command into argv to run. A ``visible`` job runs
    under ``python.exe`` (console window in the logged-on session); every
    other job stays on the silent ``pythonw.exe``.
    """
    interpreter = _launcher_python(visible=visible)
    return f'"{interpreter}" "{_launcher_py()}" run-job {job_id}'


def spawn_run_job_detached(
    job_id: str,
    run_id: str,
    trigger: str = "manual",
    params: Optional[Dict[str, Any]] = None,
    dry_run: bool = False,
) -> int:
    """Spawn ``launcher.py run-job <id> --run-id <rid> --trigger <t>`` detached.

    Used by the webapp's ``POST /api/jobs/<id>/run`` route to fire a job
    without blocking the request. Returns the spawned PID — kept only
    for diagnostics; the run record is tracked via the filesystem.

    ``params`` (issue #67) is the validated ``{name: value}`` payload from
    the run-now dialog. When present, it is JSON-encoded onto argv as
    ``--params <json>`` so the executor (which re-validates) sees an
    exact byte-for-byte copy. Schedule + Stream-Deck callers omit the
    arg entirely.

    ``dry_run`` (issue #69 'execute' mode) appends ``--dry-run`` so the
    executor spawns the child with ``JOB_DRY_RUN=1`` and stamps the run
    record. Mutex queue / chain callers never set it.
    """
    argv = [
        _launcher_python(),
        _launcher_py(),
        "run-job",
        job_id,
        "--run-id",
        run_id,
        "--trigger",
        trigger,
    ]
    if params:
        argv.extend(["--params", json.dumps(params)])
    if dry_run:
        argv.append("--dry-run")
    creationflags = _CREATE_NO_WINDOW
    detached = getattr(subprocess, "DETACHED_PROCESS", 0)
    if detached:
        creationflags |= detached
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )
    logger.info(f"🚀 spawned run-job {job_id} (rid={run_id}, pid={proc.pid})")
    return proc.pid


def task_names_for(job: Job) -> List[str]:
    """The Task Scheduler task names ``job`` materialises into.

    ``daily_times`` is the only schedule that produces more than one;
    every other type produces a single ``\\AppLauncher\\<id>``.
    """
    base = TASK_FOLDER_PREFIX + job.id
    if job.schedule.type == "daily_times" and isinstance(job.schedule.at, list):
        return [f"{base}-{i}" for i in range(1, len(job.schedule.at) + 1)]
    return [base]


def _once_schtasks_parts(at: str) -> List[str]:
    """Split ``YYYY-MM-DDTHH:MM`` into the schtasks ``/SC ONCE /SD … /ST …``
    pieces. Uses the ``YYYY/MM/DD`` slash form for ``/SD`` because it is
    accepted across Windows locales (the dotted / dashed forms are
    locale-dependent and silently fail on non-en-US systems).
    """
    date_part, _, time_part = at.partition("T")
    yyyy, mm, dd = date_part.split("-", 2)
    return [
        "/SC", "ONCE",
        "/SD", f"{yyyy}/{mm}/{dd}",
        "/ST", time_part,
    ]


def schedule_argv_parts(sched: Schedule) -> List[List[str]]:
    """The ``/SC …`` portion(s) of ``schtasks /Create`` — one per task.

    Returns an empty list for ``none``; one inner list for everything but
    ``daily_times``, which returns N (one per HH:MM). ``once`` returns a
    single inner list with ``/SC ONCE /SD <YYYY/MM/DD> /ST <HH:MM>``.
    """
    if sched.type == "none":
        return []
    if sched.type == "minutes":
        return [["/SC", "MINUTE", "/MO", str(sched.every)]]
    if sched.type == "hourly":
        return [["/SC", "HOURLY", "/MO", str(sched.every)]]
    if sched.type == "daily":
        return [["/SC", "DAILY", "/ST", str(sched.at)]]
    if sched.type == "daily_times" and isinstance(sched.at, list):
        return [["/SC", "DAILY", "/ST", str(t)] for t in sched.at]
    if sched.type == "weekly":
        return [["/SC", "WEEKLY", "/D", str(sched.day), "/ST", str(sched.at)]]
    if sched.type == "once" and isinstance(sched.at, str):
        return [_once_schtasks_parts(sched.at)]
    return []


# ----------------------------------------------------------- sync API


def list_known_tasks(
    runner: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None,
) -> List[str]:
    """All task names currently under ``\\AppLauncher\\``. Best-effort.

    A failed query (Task Scheduler service down, no permission) returns
    an empty list — the sync layer then falls back to blind deletes so
    a single read failure can't strand stale tasks forever.
    """
    runner = runner or _run_schtasks
    proc = runner(["schtasks", "/Query", "/FO", "CSV", "/NH"])
    if proc.returncode != 0 or not proc.stdout:
        return []
    names: List[str] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # CSV first column = TaskName, optionally quoted.
        first = line.split(",", 1)[0].strip().strip('"')
        if first.startswith(TASK_FOLDER_PREFIX):
            names.append(first)
    return names


def delete_schtasks(
    job_id: str,
    runner: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None,
) -> List[str]:
    """Delete every ``\\AppLauncher\\<job_id>`` and ``…-N`` variant.

    Tries a directed query first; on query failure, falls back to a
    blind delete of the bare name plus ``-1..-N`` so a transient query
    failure can't leave stale tasks behind. Returns the list of task
    names actually deleted (best-effort — schtasks errors are swallowed).
    """
    runner = runner or _run_schtasks
    targets: List[str] = []
    base = TASK_FOLDER_PREFIX + job_id
    known = list_known_tasks(runner=runner)
    if known:
        targets = [
            n
            for n in known
            if n == base or n.startswith(base + "-")
        ]
    else:
        # Blind fallback — covers the bare task + every daily_times slot.
        targets = [base] + [
            f"{base}-{i}" for i in range(1, _MAX_DAILY_TIMES_VARIANTS + 1)
        ]
    deleted: List[str] = []
    for name in targets:
        proc = runner(["schtasks", "/Delete", "/F", "/TN", name])
        if proc.returncode == 0:
            deleted.append(name)
    invalidate_next_run_cache()
    return deleted


def sync_schtasks(
    job: Job,
    runner: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None,
) -> List[str]:
    """Re-create the Task Scheduler entries for ``job`` from its schedule.

    Deletes anything currently under ``\\AppLauncher\\<job.id>*`` first,
    then creates one task per schedule slot. Returns the list of task
    names created (empty for ``schedule.type == "none"`` after the
    pre-existing tasks are deleted).

    ``job.elevated`` jobs are skipped entirely (issue #352): their real
    ``/RL HIGHEST`` entry can only be created by an already-elevated
    caller, which this webapp process never is. Deleting-then-failing-to
    recreate on every edit/pause/resume silently strands a job that the
    Jobs tab still shows as scheduled — so an elevated job's Task
    Scheduler entry is treated as externally-managed and never touched
    here; it must be registered/updated by hand from an elevated shell.
    """
    runner = runner or _run_schtasks
    if job.elevated:
        # Still delete any stale entry from a prior non-elevated schedule
        # (issue #409) — otherwise it keeps firing un-elevated on its old
        # schedule indefinitely. We just never *create* the elevated entry
        # ourselves (see docstring): that still needs an elevated shell.
        delete_schtasks(job.id, runner=runner)
        return []
    delete_schtasks(job.id, runner=runner)
    if job.schedule.type == "none":
        return []
    names = task_names_for(job)
    parts = schedule_argv_parts(job.schedule)
    if len(names) != len(parts):
        # Defensive — task_names_for and schedule_argv_parts must agree.
        logger.error(
            f"❌ schedule fan-out mismatch for job {job.id}: "
            f"names={names!r} parts={parts!r}"
        )
        return []
    tr = task_run_command(job.id, visible=job.visible)
    created: List[str] = []
    for name, schedule_part in zip(names, parts):
        argv = [
            "schtasks",
            "/Create",
            "/F",
            "/TN",
            name,
            "/TR",
            tr,
        ] + schedule_part
        proc = runner(argv)
        if proc.returncode == 0:
            created.append(name)
        else:
            logger.warning(
                f"⚠️  schtasks create failed for {name}: "
                f"rc={proc.returncode} stderr={proc.stderr!r}"
            )
    invalidate_next_run_cache()
    return created


_NEXT_RUN_RE = re.compile(
    r"^Next Run Time:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE
)
_TASK_NAME_RE = re.compile(
    r"^TaskName:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE
)


def _parse_bulk_query(stdout: str) -> Dict[str, Optional[str]]:
    """Parse ``schtasks /Query /FO LIST /V`` into ``{task_name: next_run}``.

    Each task record is a block of ``Key: Value`` lines separated from
    the next by blank line(s). We walk records, pluck the first
    ``TaskName:`` and ``Next Run Time:`` we find, and keep only entries
    under ``\\AppLauncher\\`` so foreign tasks never leak into the cache.
    """
    out: Dict[str, Optional[str]] = {}
    block: Dict[str, str] = {}

    def commit(b: Dict[str, str]) -> None:
        name = b.get("TaskName", "").strip()
        if not name.startswith(TASK_FOLDER_PREFIX):
            return
        next_run = b.get("Next Run Time", "").strip()
        # schtasks renders missing / disabled as "N/A" or "Disabled" —
        # both collapse to None at the UI layer.
        if not next_run or next_run.upper() in {"N/A", "DISABLED"}:
            out[name] = None
        else:
            out[name] = next_run

    for raw in stdout.splitlines():
        line = raw.rstrip()
        if not line:
            if block:
                commit(block)
                block = {}
            continue
        # New TaskName line ends the previous record (schtasks LIST output
        # has no consistent blank-line separator on all locales).
        m = _TASK_NAME_RE.match(line)
        if m and block.get("TaskName"):
            commit(block)
            block = {}
        if ":" in line:
            key, _, value = line.partition(":")
            block[key.strip()] = value.strip()
    if block:
        commit(block)
    return out


def _bulk_next_runs(
    runner: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None,
) -> Dict[str, Optional[str]]:
    """One ``schtasks /Query /FO LIST /V`` covering every AppLauncher task."""
    runner = runner or _run_schtasks
    proc = runner(["schtasks", "/Query", "/FO", "LIST", "/V"])
    if proc.returncode != 0 or not proc.stdout:
        return {}
    return _parse_bulk_query(proc.stdout)


def _cached_bulk_next_runs(
    runner: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None,
) -> Dict[str, Optional[str]]:
    """Return the bulk map, refreshing the process-local cache on TTL miss."""
    global _next_run_cache
    now = time.monotonic()
    with _next_run_lock:
        if _next_run_cache is not None:
            ts, snapshot = _next_run_cache
            if now - ts < _NEXT_RUN_TTL_SECONDS:
                return snapshot
        fresh = _bulk_next_runs(runner=runner)
        _next_run_cache = (now, fresh)
        return fresh


def invalidate_next_run_cache() -> None:
    """Drop the cached schtasks snapshot.

    Called after ``sync_schtasks`` / ``delete_schtasks`` so a Task
    Scheduler edit shows up on the next ``/api/jobs`` poll instead of
    waiting out the TTL.
    """
    global _next_run_cache
    with _next_run_lock:
        _next_run_cache = None


def query_next_run(
    job_id: str,
    runner: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None,
) -> Optional[str]:
    """Best-effort: the earliest 'Next Run Time' across this job's tasks.

    Backed by a 30 s process-local cache of one bulk ``schtasks /Query``
    call (see :func:`_cached_bulk_next_runs`). Returns ``None`` when no
    task exists, the field is ``N/A``, or the query failed entirely. The
    string is the raw schtasks rendering — the UI is responsible for
    localisation tidying.
    """
    snapshot = _cached_bulk_next_runs(runner=runner)
    base = TASK_FOLDER_PREFIX + job_id
    candidates: List[str] = []
    for name, next_run in snapshot.items():
        if name != base and not name.startswith(base + "-"):
            continue
        if next_run:
            candidates.append(next_run)
    # Sort lexicographically — schtasks renders the locale-default
    # date/time string, so this is a best-effort "earliest"; the legacy
    # code's first-hit behaviour was no better. UI shows the picked
    # string verbatim either way.
    candidates.sort()
    return candidates[0] if candidates else None


# ------------------------------------------------------- computed next fire
#
# The schtasks "Next Run Time" string above is a locale-formatted, lexically
# sorted best-effort value — fine to *display*, useless to *sort by* or to
# turn into a countdown. The schedule definition, however, is a small
# deterministic set (see src.jobs_config), so we compute the next wall-clock
# fire ourselves. This is the field the UI sorts on and renders "in 3h" from.

# Day-name → datetime.weekday() index (Mon=0 .. Sun=6).
_WEEKDAY_INDEX = {
    "MON": 0,
    "TUE": 1,
    "WED": 2,
    "THU": 3,
    "FRI": 4,
    "SAT": 5,
    "SUN": 6,
}


def _hhmm(value: Any) -> Optional[Tuple[int, int]]:
    """Parse ``"HH:MM"`` → ``(hour, minute)``, or ``None`` when malformed."""
    if not isinstance(value, str):
        return None
    try:
        hh, mm = value.split(":", 1)
        h, m = int(hh), int(mm)
    except ValueError:
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return h, m
    return None


def next_fire(
    sched: Schedule, *, now: Optional[datetime] = None
) -> Optional[datetime]:
    """The next wall-clock fire time for ``sched``, computed from its shape.

    Pure + deterministic — derived from the bounded schedule definition,
    not from schtasks. Returns ``None`` for ``none`` (which includes a
    *paused* job, whose active schedule is parked as ``none`` while the
    real shape lives in ``paused_schedule``) and for a ``once`` schedule
    that has already elapsed. ``now`` is injectable for testing.

    Computed in local naive time: the launcher and Task Scheduler both run
    in the logged-on session's local time, so this matches what the user
    sees and what actually fires.
    """
    now = now or datetime.now()
    t = sched.type
    if t == "minutes" and isinstance(sched.every, int) and sched.every > 0:
        return now + timedelta(minutes=sched.every)
    if t == "hourly" and isinstance(sched.every, int) and sched.every > 0:
        return now + timedelta(hours=sched.every)
    if t == "daily":
        hm = _hhmm(sched.at)
        if hm is None:
            return None
        candidate = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    if t == "daily_times" and isinstance(sched.at, list):
        best: Optional[datetime] = None
        for entry in sched.at:
            hm = _hhmm(entry)
            if hm is None:
                continue
            candidate = now.replace(
                hour=hm[0], minute=hm[1], second=0, microsecond=0
            )
            if candidate <= now:
                candidate += timedelta(days=1)
            if best is None or candidate < best:
                best = candidate
        return best
    if t == "weekly" and sched.day in _WEEKDAY_INDEX:
        hm = _hhmm(sched.at)
        if hm is None:
            return None
        candidate = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
        days_ahead = (_WEEKDAY_INDEX[sched.day] - now.weekday()) % 7
        candidate += timedelta(days=days_ahead)
        if candidate <= now:
            candidate += timedelta(days=7)
        return candidate
    if t == "once" and isinstance(sched.at, str):
        try:
            fire = datetime.fromisoformat(sched.at)
        except ValueError:
            return None
        return fire if fire > now else None
    # "none" and any malformed shape fall through to no next fire.
    return None


# Schedule types whose cadence is too dense to enumerate over an agenda
# window (issue #230). The agenda summarises these as a single "frequent"
# row instead of one entry per fire. next_fire never returns None for
# them, so upcoming_fires must short-circuit before the enumeration loop.
FREQUENT_SCHEDULE_TYPES = frozenset({"minutes", "hourly"})


def upcoming_fires(
    sched: Schedule, *, start: datetime, end: datetime, cap: int = 200
) -> List[datetime]:
    """Every fire of ``sched`` in the half-open window ``[start, end)``.

    Built by walking :func:`next_fire` forward — each call with
    ``now=cursor`` returns a fire strictly after ``cursor`` (the
    ``candidate <= now`` roll-forward guarantees it), so advancing the
    cursor to each result enumerates the window without re-deriving any
    cadence math (issue #230 reuses #229's tested helper).

    Returns ``[]`` for ``none`` / already-elapsed ``once`` and for the
    dense :data:`FREQUENT_SCHEDULE_TYPES` (``minutes`` / ``hourly``),
    which the agenda summarises rather than expands. ``cap`` bounds the
    list defensively (a 3-slot ``daily_times`` over a week is ~21).
    """
    if sched.type in FREQUENT_SCHEDULE_TYPES:
        return []
    fires: List[datetime] = []
    cursor = start
    while len(fires) < cap:
        nf = next_fire(sched, now=cursor)
        if nf is None or nf >= end:
            break
        fires.append(nf)
        cursor = nf
    return fires
