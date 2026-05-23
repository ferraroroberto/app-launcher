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
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.jobs_config import Job, Schedule

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

TASK_NAMESPACE = "AppLauncher"
TASK_FOLDER_PREFIX = f"\\{TASK_NAMESPACE}\\"

JOBS_RUNS_DIR = PROJECT_ROOT / "webapp" / "jobs"
MAX_RUNS_PER_JOB = 20

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
    job_id: str, run_id: str, trigger: str = "manual"
) -> int:
    """Spawn ``launcher.py run-job <id> --run-id <rid> --trigger <t>`` detached.

    Used by the webapp's ``POST /api/jobs/<id>/run`` route to fire a job
    without blocking the request. Returns the spawned PID — kept only
    for diagnostics; the run record is tracked via the filesystem.
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
    return created


_NEXT_RUN_RE = re.compile(
    r"^Next Run Time:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE
)


def query_next_run(
    job_id: str,
    runner: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None,
) -> Optional[str]:
    """Best-effort: the earliest 'Next Run Time' across this job's tasks.

    Returns ``None`` when no task exists, the field is ``"N/A"``, or the
    query fails. The string is the raw schtasks rendering — the UI is
    responsible for any localisation tidying.
    """
    runner = runner or _run_schtasks
    known = list_known_tasks(runner=runner)
    base = TASK_FOLDER_PREFIX + job_id
    matches = [n for n in known if n == base or n.startswith(base + "-")]
    if not matches:
        return None
    candidates: List[str] = []
    for name in matches:
        proc = runner(["schtasks", "/Query", "/TN", name, "/FO", "LIST", "/V"])
        if proc.returncode != 0 or not proc.stdout:
            continue
        for hit in _NEXT_RUN_RE.findall(proc.stdout):
            value = hit.strip()
            if value and value.upper() != "N/A":
                candidates.append(value)
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
