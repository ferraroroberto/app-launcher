"""Windows Task Scheduler sync + run-history file storage for the Jobs tab.

Two responsibilities, packaged together because they share the namespace:

* ``schtasks`` sync — every :class:`~src.jobs_config.Job` with a non-``none``
  schedule materialises as one or more entries under the
  ``\\AppLauncher\\`` Task Scheduler folder. ``daily_times`` jobs fan
  out into ``\\AppLauncher\\<id>-1``, ``…-2``, … so a single schedule
  with three wake-ups becomes three Task Scheduler entries pointing at
  the same executor.

* Run-history files — every run (manual or scheduled) creates
  ``webapp/jobs/<job_id>/<run_id>/`` with ``run.json`` (metadata) and
  ``output.log`` (combined stdout+stderr). Mirrors the audit pattern in
  :mod:`src.audit`. Pruned to :data:`MAX_RUNS_PER_JOB` per job.

The single executor that ever runs a job is
:class:`~app.cli.commands.run_job_cmd.RunJobCommand`. Task Scheduler
calls it with ``pythonw launcher.py run-job <id>``; the webapp's
``POST /api/jobs/<id>/run`` route spawns the same command detached and
returns the new ``run_id`` immediately.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import statistics
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

JOBS_RUNS_DIR = PROJECT_ROOT / "webapp" / "jobs"
MAX_RUNS_PER_JOB = 20

# Per-mutex-group FIFO queue (issue #68 PR #2). One JSON file keyed by
# group → list-of-entries. Atomic via `os.replace` for each mutation.
JOBS_QUEUE_PATH = JOBS_RUNS_DIR / "_queue.json"
_queue_lock = Lock()

# Process-local TTL caches. The original Jobs-tab v1 shelled out to
# schtasks once per job, per /api/jobs poll (every 3 s while the tab
# was open) — N+1 fork+exec on Windows for what is effectively a static
# schedule. Both caches live inside the webapp process and are reset by
# `invalidate_next_run_cache` whenever sync/delete writes change Task
# Scheduler state.
_NEXT_RUN_TTL_SECONDS = 30.0
_STATS_TTL_SECONDS = 30.0
_next_run_cache: Optional[Tuple[float, Dict[str, Optional[str]]]] = None
_next_run_lock = Lock()
_stats_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_stats_lock = Lock()

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Defensive upper bound when blind-deleting daily_times variants without
# a query first. 24 covers every hour of the day with headroom.
_MAX_DAILY_TIMES_VARIANTS = 24


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


def _pythonw_path() -> str:
    """The launcher's own venv ``pythonw.exe``, with PATH fallback."""
    candidate = PROJECT_ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if candidate.is_file():
        return str(candidate)
    return "pythonw.exe"


def _launcher_py() -> str:
    return str(PROJECT_ROOT / "launcher.py")


def task_run_command(job_id: str) -> str:
    """The /TR string Task Scheduler stores for ``job_id``.

    Quoted so paths-with-spaces survive Task Scheduler's tokenisation
    when it splits the command into argv to run.
    """
    return f'"{_pythonw_path()}" "{_launcher_py()}" run-job {job_id}'


def spawn_run_job_detached(
    job_id: str,
    run_id: str,
    trigger: str = "manual",
    params: Optional[Dict[str, Any]] = None,
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
    """
    argv = [
        _pythonw_path(),
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


def schedule_argv_parts(sched: Schedule) -> List[List[str]]:
    """The ``/SC …`` portion(s) of ``schtasks /Create`` — one per task.

    Returns an empty list for ``none``; one inner list for everything but
    ``daily_times``, which returns N (one per HH:MM).
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
    """
    runner = runner or _run_schtasks
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
    tr = task_run_command(job.id)
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


# ----------------------------------------------------------- run history


def runs_dir(job_id: str) -> Path:
    """Where this job's run history lives."""
    return JOBS_RUNS_DIR / job_id


def new_run_id() -> str:
    """A sortable, filesystem-safe run id."""
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def new_run_dir(job_id: str, run_id: Optional[str] = None) -> Path:
    """Create and return ``webapp/jobs/<job_id>/<run_id>/``.

    Collisions are resolved by appending ``-2``, ``-3``, … to ``run_id``
    so two manual triggers within the same second never overwrite each
    other.
    """
    base = runs_dir(job_id)
    base.mkdir(parents=True, exist_ok=True)
    rid = run_id or new_run_id()
    target = base / rid
    n = 2
    while target.exists():
        target = base / f"{rid}-{n}"
        n += 1
    target.mkdir()
    return target


def write_run_json(run_dir: Path, **fields: Any) -> None:
    """Atomic write of ``run_dir / run.json`` — merges with existing fields."""
    target = run_dir / "run.json"
    existing: Dict[str, Any] = {}
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
    existing.update({k: v for k, v in fields.items() if v is not None})
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def read_run(run_dir: Path) -> Dict[str, Any]:
    """Read ``run.json`` from ``run_dir``. Missing file → empty dict."""
    target = run_dir / "run.json"
    if not target.exists():
        return {}
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def list_runs(job_id: str) -> List[Dict[str, Any]]:
    """Newest-first list of decorated run records for ``job_id``."""
    base = runs_dir(job_id)
    if not base.is_dir():
        return []
    runs: List[Dict[str, Any]] = []
    for child in sorted(base.iterdir(), reverse=True):
        if not child.is_dir():
            continue
        run = read_run(child)
        run.setdefault("run_id", child.name)
        runs.append(run)
    return runs


def latest_run(job_id: str) -> Optional[Dict[str, Any]]:
    runs = list_runs(job_id)
    return runs[0] if runs else None


def is_running(job_id: str) -> bool:
    """Cheap check — is the most recent run still in ``running`` state?"""
    latest = latest_run(job_id)
    return bool(latest and latest.get("status") == "running")


# ----------------------------------------------------------- mutex queue
#
# Cross-job admission control (issue #68 PR #2). When a job carries a
# ``mutex_group`` and another job in the same group is ``running`` or
# ``pending``, the fresh fire is queued rather than rejected. The
# finalising executor pops the next entry on its way out and spawns it
# detached. Queue file lives at ``JOBS_QUEUE_PATH`` (one JSON document,
# keyed by group → FIFO list of pending entries).


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
    tmp = JOBS_QUEUE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(pruned, indent=2), encoding="utf-8")
    os.replace(tmp, JOBS_QUEUE_PATH)


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
        if run.get("status") == "skipped":
            continue
        anchor = run
        break
    if anchor is None:
        return None
    started_raw = anchor.get("started_at")
    if not isinstance(started_raw, str) or not started_raw:
        return None
    try:
        started = datetime.fromisoformat(started_raw)
    except ValueError:
        return None
    reference = now or datetime.now()
    elapsed = (reference - started).total_seconds()
    remaining = cooldown - elapsed
    if remaining <= 0:
        return None
    return int(math.ceil(remaining)), cooldown, str(anchor.get("run_id") or "")


def prune_runs(job_id: str, keep: int = MAX_RUNS_PER_JOB) -> int:
    """Delete the oldest run dirs beyond ``keep``. Returns count removed."""
    base = runs_dir(job_id)
    if not base.is_dir():
        return 0
    children = [c for c in base.iterdir() if c.is_dir()]
    # Newest first by name — run ids are sortable timestamps.
    children.sort(key=lambda p: p.name, reverse=True)
    removed = 0
    for child in children[keep:]:
        try:
            for f in child.iterdir():
                try:
                    f.unlink()
                except OSError:
                    pass
            child.rmdir()
            removed += 1
        except OSError as exc:
            logger.debug(f"prune_runs: could not remove {child}: {exc}")
    return removed


# ----------------------------------------------------------- stats / health


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


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


def is_stuck(
    job_id: str, *, p95_factor: float = 3.0, floor_seconds: float = 300.0
) -> bool:
    """``True`` when the latest run is ``running`` past a sane threshold.

    Threshold = ``max(p95 × factor, floor_seconds)``. Surface-only — the
    UI shows ⚠️ and exposes a manual kill button; no auto-kill.
    """
    latest = latest_run(job_id)
    if not latest or latest.get("status") != "running":
        return False
    started = _parse_iso(latest.get("started_at"))
    if not started:
        return False
    stats = run_stats(job_id, fresh=True)
    p95 = stats.get("p95") or 0.0
    threshold = max(p95 * p95_factor, floor_seconds)
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


# ----------------------------------------------------------- output tail


def read_output_tail(run_dir: Path, max_bytes: int = 64 * 1024) -> str:
    """Read up to the last ``max_bytes`` of ``output.log``. Missing → ``""``."""
    target = run_dir / "output.log"
    if not target.is_file():
        return ""
    try:
        size = target.stat().st_size
        with target.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                # Drop a (possibly partial) first line after the seek.
                fh.readline()
            data = fh.read()
        return data.decode("utf-8", errors="replace")
    except OSError as exc:
        logger.debug(f"read_output_tail({target}) failed: {exc}")
        return ""
