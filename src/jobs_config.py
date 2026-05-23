"""Jobs registry — load, save, mutate ``config/jobs.json``.

The Jobs tab is the launcher's third surface (next to Coding and Apps).
A *job* is a one-shot script that any of three triggers can fire: a
phone tap (``POST /api/jobs/<id>/run``), a Stream Deck button (the same
HTTP call), or a schedule (Windows Task Scheduler). All three funnel
through the single executor :mod:`app.cli.commands.run_job_cmd`, so
every run produces a uniform run record under ``webapp/jobs/<id>/<rid>/``.

The file is one JSON document, gitignored, with a committed
``config/jobs.sample.json`` template:

    {
      "jobs": [
        {
          "id": "reporting-daily",
          "name": "Daily Reporting",
          "script_path": "E:\\\\automation\\\\reporting\\\\launch_reporting.bat",
          "args": "auto",
          "schedule": {"type": "daily", "at": "06:00"},
          "added_at": "2026-05-23T..."
        }
      ]
    }

``script_path`` accepts either a ``.py`` Python script or a ``.bat``
Windows batch file. The executor dispatches on the suffix (see
``run_job_cmd``).

Schedule types are a bounded set — no raw cron expressions:

* ``none``           — manual only, no scheduled run
* ``minutes``        — every N minutes               (``every: int``)
* ``hourly``         — every N hours                 (``every: int``)
* ``daily``          — once a day at HH:MM           (``at: "HH:MM"``)
* ``daily_times``    — N times a day at HH:MM list   (``at: ["HH:MM",…]``)
* ``weekly``         — once a week                   (``day: "MON|…"``, ``at: "HH:MM"``)
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from src.scanner import slugify

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JOBS_PATH = PROJECT_ROOT / "config" / "jobs.json"

# Bounded set of schedule types. Anything else fails validation.
SCHEDULE_TYPES = frozenset(
    {"none", "minutes", "hourly", "daily", "daily_times", "weekly"}
)

# schtasks accepts MON|TUE|WED|THU|FRI|SAT|SUN (uppercase, three-letter).
WEEKLY_DAYS = frozenset({"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"})

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


# ---------------------------------------------------------------- Schedule


@dataclass
class Schedule:
    """A job's trigger cadence — see module docstring for the bounded set."""

    type: str = "none"
    every: Optional[int] = None
    at: Union[str, List[str], None] = None
    day: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"type": self.type}
        if self.every is not None:
            payload["every"] = self.every
        if self.at is not None:
            payload["at"] = self.at
        if self.day is not None:
            payload["day"] = self.day
        return payload

    def chip(self) -> str:
        """Compact human label for the UI ('daily 06:00', 'every 5 min', …)."""
        if self.type == "none":
            return ""
        if self.type == "minutes":
            return f"every {self.every} min"
        if self.type == "hourly":
            return f"every {self.every} h"
        if self.type == "daily":
            return f"daily {self.at}"
        if self.type == "daily_times" and isinstance(self.at, list):
            return "daily " + " ".join(self.at)
        if self.type == "weekly":
            return f"{self.day} {self.at}"
        return self.type


def _validate_schedule(sched: Schedule) -> None:
    """Raise ``ValueError`` if ``sched`` is malformed for its type."""
    if sched.type not in SCHEDULE_TYPES:
        raise ValueError(f"unknown schedule type: {sched.type!r}")
    if sched.type == "none":
        return
    if sched.type in ("minutes", "hourly"):
        if not isinstance(sched.every, int) or sched.every <= 0:
            raise ValueError(
                f"schedule {sched.type!r} requires every > 0, got {sched.every!r}"
            )
        if sched.type == "hourly" and sched.every > 23:
            # schtasks /SC HOURLY /MO accepts 1..23.
            raise ValueError("hourly schedule every must be 1..23")
        return
    if sched.type == "daily":
        if not isinstance(sched.at, str) or not _HHMM_RE.match(sched.at):
            raise ValueError(f"daily schedule needs at=HH:MM, got {sched.at!r}")
        return
    if sched.type == "daily_times":
        if not isinstance(sched.at, list) or not sched.at:
            raise ValueError("daily_times schedule needs a non-empty at list")
        for t in sched.at:
            if not isinstance(t, str) or not _HHMM_RE.match(t):
                raise ValueError(f"daily_times entry must be HH:MM, got {t!r}")
        return
    if sched.type == "weekly":
        if sched.day not in WEEKLY_DAYS:
            raise ValueError(
                f"weekly schedule day must be one of {sorted(WEEKLY_DAYS)}"
            )
        if not isinstance(sched.at, str) or not _HHMM_RE.match(sched.at):
            raise ValueError(f"weekly schedule needs at=HH:MM, got {sched.at!r}")


def schedule_from_dict(raw: Any) -> Schedule:
    """Parse a JSON-shape schedule, raising ``ValueError`` on malformed input."""
    if raw is None:
        return Schedule(type="none")
    if not isinstance(raw, dict):
        raise ValueError(f"schedule must be an object, got {type(raw).__name__}")
    sched = Schedule(
        type=str(raw.get("type") or "none"),
        every=raw.get("every"),
        at=raw.get("at"),
        day=(str(raw["day"]).upper() if raw.get("day") else None),
    )
    _validate_schedule(sched)
    return sched


# -------------------------------------------------------------------- Job


@dataclass
class Job:
    id: str
    name: str
    script_path: str
    args: str = ""
    schedule: Schedule = field(default_factory=Schedule)
    added_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "script_path": self.script_path,
            "args": self.args,
            "schedule": self.schedule.to_dict(),
            "added_at": self.added_at,
        }

    @property
    def target_kind(self) -> str:
        """``"py"`` or ``"bat"`` based on ``script_path`` suffix."""
        suffix = Path(self.script_path).suffix.lower()
        if suffix == ".py":
            return "py"
        if suffix == ".bat":
            return "bat"
        return "unknown"


def job_from_dict(raw: Dict[str, Any]) -> Job:
    """Build a :class:`Job` from one JSON row. Raises on invalid input."""
    script_path = str(raw.get("script_path") or "").strip()
    if not script_path:
        raise ValueError("script_path is required")
    suffix = Path(script_path).suffix.lower()
    if suffix not in (".py", ".bat"):
        raise ValueError(
            f"script_path must end .py or .bat, got {script_path!r}"
        )
    job = Job(
        id=str(raw.get("id") or "").strip(),
        name=str(raw.get("name") or "").strip(),
        script_path=script_path,
        args=str(raw.get("args") or ""),
        schedule=schedule_from_dict(raw.get("schedule")),
        added_at=str(raw.get("added_at") or ""),
    )
    if not job.id:
        raise ValueError("job id is required")
    if not job.name:
        raise ValueError("job name is required")
    return job


def make_job_id(name: str, existing_ids: Optional[List[str]] = None) -> str:
    """Slugify ``name`` into a job id, suffixing to avoid collisions."""
    base = slugify(name) or "job"
    if not existing_ids:
        return base
    have = set(existing_ids)
    if base not in have:
        return base
    n = 2
    while f"{base}-{n}" in have:
        n += 1
    return f"{base}-{n}"


# ----------------------------------------------------------- JobsConfig


@dataclass
class JobsConfig:
    jobs: List[Job] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"jobs": [j.to_dict() for j in self.jobs]}


def load_jobs(path: Optional[Path] = None) -> JobsConfig:
    """Read ``config/jobs.json`` into a :class:`JobsConfig`.

    Missing file → empty config. Malformed file → empty config + warning
    (the launcher must keep booting). Individual malformed rows are
    skipped with a warning; the rest of the file is kept.
    """
    target = Path(path) if path is not None else DEFAULT_JOBS_PATH
    if not target.exists():
        return JobsConfig()

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"⚠️  Could not read {target} ({exc}); starting fresh")
        return JobsConfig()

    jobs: List[Job] = []
    for row in raw.get("jobs") or []:
        if not isinstance(row, dict):
            continue
        try:
            jobs.append(job_from_dict(row))
        except ValueError as exc:
            logger.warning(f"⚠️  Skipping malformed job row: {exc} ({row!r})")
    return JobsConfig(jobs=jobs)


def save_jobs(cfg: JobsConfig, path: Optional[Path] = None) -> Path:
    """Persist ``cfg`` to disk via an atomic ``.tmp`` swap."""
    target = Path(path) if path is not None else DEFAULT_JOBS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg.to_dict(), indent=2), encoding="utf-8")
    os.replace(tmp, target)
    return target


# ----------------------------------------------------------- mutations


def get_by_id(cfg: JobsConfig, job_id: str) -> Optional[Job]:
    return next((j for j in cfg.jobs if j.id == job_id), None)


def add_job(cfg: JobsConfig, job: Job) -> Job:
    """Append ``job`` and persist. Raises ``ValueError`` on duplicate id."""
    if any(j.id == job.id for j in cfg.jobs):
        raise ValueError(f"job id already exists: {job.id}")
    if not job.added_at:
        job.added_at = datetime.now().isoformat(timespec="seconds")
    cfg.jobs.append(job)
    cfg.jobs.sort(key=lambda j: j.name.lower())
    save_jobs(cfg)
    return job


def update_job(cfg: JobsConfig, job_id: str, **fields: Any) -> Optional[Job]:
    """In-place edit. Accepts ``name``, ``script_path``, ``args``, ``schedule``."""
    job = get_by_id(cfg, job_id)
    if job is None:
        return None
    if "name" in fields and fields["name"]:
        job.name = str(fields["name"]).strip()
    if "script_path" in fields and fields["script_path"]:
        sp = str(fields["script_path"]).strip()
        if Path(sp).suffix.lower() not in (".py", ".bat"):
            raise ValueError(f"script_path must end .py or .bat, got {sp!r}")
        job.script_path = sp
    if "args" in fields:
        job.args = str(fields["args"] or "")
    if "schedule" in fields:
        job.schedule = schedule_from_dict(fields["schedule"])
    cfg.jobs.sort(key=lambda j: j.name.lower())
    save_jobs(cfg)
    return job


def remove_by_id(cfg: JobsConfig, job_id: str) -> Optional[Job]:
    for i, job in enumerate(cfg.jobs):
        if job.id == job_id:
            removed = cfg.jobs.pop(i)
            save_jobs(cfg)
            return removed
    return None
