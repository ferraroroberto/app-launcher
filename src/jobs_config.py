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
          "script_path": "E:\\\\automation\\\\content-management\\\\launch_reporting.bat",
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

# Bounded set of typed-parameter kinds (issue #67). Anything else fails
# validation. Mirrors the same closed-set discipline as SCHEDULE_TYPES.
PARAM_KINDS = frozenset({"string", "int", "enum", "bool", "date"})

_PARAM_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_PARAM_FLAG_RE = re.compile(r"^--[a-zA-Z][a-zA-Z0-9_-]*$")
_PARAM_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_PARAM_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Upper bound on cooldown — a full day. Anything past this is almost
# certainly a typo (e.g. ms thought of as seconds) and the rest of the
# stack would render it confusingly.
MAX_COOLDOWN_SECONDS = 86_400

# Mutex group identifier — lowercase, alnum + hyphen/underscore, 1..32
# chars. Same conservative shape as the job id slug; intentionally not a
# free-form string so the UI can show it back as a pill without escaping
# and the queue-file key stays filesystem-safe (even though the queue is
# one file with the group as a JSON key, not a dir name).
MAX_MUTEX_GROUP_LEN = 32
_MUTEX_GROUP_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


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


# ------------------------------------------------------------------ Param


@dataclass
class Param:
    """One typed input declaration for a job (issue #67).

    Used by ``src.jobs_argv.compose_argv`` at run-time to validate user
    input and project it into argv/env. Kind is closed (see
    :data:`PARAM_KINDS`); the editor / run-now dialog renders inputs
    from these declarations.
    """

    name: str
    kind: str
    default: Any = None
    required: bool = True
    options: Optional[List[str]] = None
    flag: Optional[str] = None
    env: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"name": self.name, "kind": self.kind}
        # Required is the common case; only emit it explicitly when False
        # so historical configs round-trip cleanly.
        if not self.required:
            payload["required"] = False
        if self.default is not None:
            payload["default"] = self.default
        if self.options is not None:
            payload["options"] = list(self.options)
        if self.flag:
            payload["flag"] = self.flag
        if self.env:
            payload["env"] = self.env
        return payload


def _validate_default(name: str, kind: str, default: Any,
                      options: Optional[List[str]]) -> Any:
    """Type-check ``default`` against ``kind`` and return it (coerced)."""
    if kind == "string":
        if not isinstance(default, str):
            raise ValueError(
                f"param {name!r}: default must be a string, got {type(default).__name__}"
            )
        return default
    if kind == "int":
        # bool is a subclass of int — reject explicitly to avoid accidents.
        if isinstance(default, bool) or not isinstance(default, int):
            raise ValueError(
                f"param {name!r}: default must be an int, got {type(default).__name__}"
            )
        return default
    if kind == "bool":
        if not isinstance(default, bool):
            raise ValueError(
                f"param {name!r}: default must be true/false, got {default!r}"
            )
        return default
    if kind == "enum":
        if not isinstance(default, str) or default not in (options or []):
            raise ValueError(
                f"param {name!r}: default {default!r} must be one of {options!r}"
            )
        return default
    if kind == "date":
        if not isinstance(default, str) or not _PARAM_DATE_RE.match(default):
            raise ValueError(
                f"param {name!r}: default must be YYYY-MM-DD, got {default!r}"
            )
        return default
    raise ValueError(f"param {name!r}: unsupported kind {kind!r}")


def param_from_dict(raw: Any) -> Param:
    """Parse + validate one ``Param`` row. Raises ``ValueError`` on bad input.

    Validation is the only place these rules live; ``compose_argv`` trusts
    the resulting :class:`Param` and the router translates ``ValueError``
    into HTTP 400.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"param row must be an object, got {type(raw).__name__}")

    name = str(raw.get("name") or "").strip()
    if not _PARAM_NAME_RE.match(name):
        raise ValueError(
            f"param name {name!r} must be snake_case (start with a letter)"
        )

    kind = str(raw.get("kind") or "").strip()
    if kind not in PARAM_KINDS:
        raise ValueError(
            f"param {name!r}: kind must be one of {sorted(PARAM_KINDS)}, got {kind!r}"
        )

    # options: required for enum, rejected for other kinds.
    raw_options = raw.get("options")
    options: Optional[List[str]] = None
    if kind == "enum":
        if not isinstance(raw_options, list) or not raw_options:
            raise ValueError(
                f"param {name!r}: kind=enum requires a non-empty options list"
            )
        if not all(isinstance(o, str) and o for o in raw_options):
            raise ValueError(
                f"param {name!r}: options must be non-empty strings"
            )
        # Defensive de-dup while preserving order — a duplicate enum slot
        # is almost certainly a user typo and confuses the UI dropdown.
        seen: set = set()
        deduped: List[str] = []
        for o in raw_options:
            if o in seen:
                raise ValueError(
                    f"param {name!r}: duplicate option {o!r}"
                )
            seen.add(o)
            deduped.append(o)
        options = deduped
    elif raw_options not in (None, []):
        raise ValueError(
            f"param {name!r}: options only valid for kind=enum"
        )

    flag = raw.get("flag")
    if flag is not None:
        if not isinstance(flag, str) or not _PARAM_FLAG_RE.match(flag):
            raise ValueError(
                f"param {name!r}: flag {flag!r} must look like --foo"
            )

    env = raw.get("env")
    if env is not None:
        if not isinstance(env, str) or not _PARAM_ENV_RE.match(env):
            raise ValueError(
                f"param {name!r}: env {env!r} must be UPPER_SNAKE_CASE"
            )

    if flag and env:
        raise ValueError(
            f"param {name!r}: flag and env are mutually exclusive"
        )

    # bool without a flag or env has no useful representation — emit-as-
    # positional would produce a literal "true"/"false" argv entry, which
    # is footgun-y and not used anywhere in this repo.
    if kind == "bool" and not flag and not env:
        raise ValueError(
            f"param {name!r}: kind=bool requires either a flag or an env mapping"
        )

    default = raw.get("default")
    if default is not None:
        default = _validate_default(name, kind, default, options)

    # required defaults to True unless a default is present, in which case
    # absence is fine. Explicit "required" in raw wins over the heuristic.
    if "required" in raw:
        required = bool(raw["required"])
    else:
        required = default is None

    return Param(
        name=name,
        kind=kind,
        default=default,
        required=required,
        options=options,
        flag=(flag or None),
        env=(env or None),
    )


def params_from_dict(raw: Any) -> List[Param]:
    """Parse a list of param rows. Empty / missing → ``[]``."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"params must be a list, got {type(raw).__name__}")
    result: List[Param] = []
    names: set = set()
    for row in raw:
        param = param_from_dict(row)
        if param.name in names:
            raise ValueError(f"duplicate param name: {param.name!r}")
        names.add(param.name)
        result.append(param)
    return result


# -------------------------------------------------------------------- Job


@dataclass
class Job:
    id: str
    name: str
    script_path: str
    args: str = ""
    schedule: Schedule = field(default_factory=Schedule)
    added_at: str = ""
    params: List[Param] = field(default_factory=list)
    cooldown_seconds: Optional[int] = None
    mutex_group: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "script_path": self.script_path,
            "args": self.args,
            "schedule": self.schedule.to_dict(),
            "added_at": self.added_at,
        }
        # Only emit params when non-empty so legacy jobs.json rows survive
        # a load → save round-trip without sprouting empty arrays.
        if self.params:
            payload["params"] = [p.to_dict() for p in self.params]
        # cooldown_seconds: omit when unset / zero so legacy rows stay
        # byte-for-byte after a load → save round-trip.
        if self.cooldown_seconds:
            payload["cooldown_seconds"] = self.cooldown_seconds
        if self.mutex_group:
            payload["mutex_group"] = self.mutex_group
        return payload

    @property
    def target_kind(self) -> str:
        """``"py"`` or ``"bat"`` based on ``script_path`` suffix."""
        suffix = Path(self.script_path).suffix.lower()
        if suffix == ".py":
            return "py"
        if suffix == ".bat":
            return "bat"
        return "unknown"


def _validate_cooldown(raw: Any) -> Optional[int]:
    """Parse + validate ``cooldown_seconds`` for a job row.

    Accepts ``None`` / missing / explicit ``0`` (all collapse to "no
    cooldown" → ``None``). Otherwise must be an ``int`` in ``[1,
    MAX_COOLDOWN_SECONDS]``. Bool is rejected explicitly because
    ``bool`` is a subclass of ``int`` in Python.
    """
    if raw is None or raw == 0:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(
            f"cooldown_seconds must be a non-negative int, got "
            f"{type(raw).__name__}"
        )
    if raw < 0:
        raise ValueError(
            f"cooldown_seconds must be >= 0, got {raw}"
        )
    if raw > MAX_COOLDOWN_SECONDS:
        raise ValueError(
            f"cooldown_seconds must be <= {MAX_COOLDOWN_SECONDS}, got {raw}"
        )
    return raw


def _validate_mutex_group(raw: Any) -> Optional[str]:
    """Parse + validate ``mutex_group``. Empty / missing → ``None``.

    Shape mirrors a slug — lowercase, alnum + ``_`` or ``-``, must start
    with a letter, max 32 chars. Conservative on purpose: the value
    appears verbatim in UI pills and is used as a JSON key in the queue
    file, so we keep it boring and predictable.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(
            f"mutex_group must be a string, got {type(raw).__name__}"
        )
    stripped = raw.strip()
    if not stripped:
        return None
    if not _MUTEX_GROUP_RE.match(stripped):
        raise ValueError(
            f"mutex_group {stripped!r} must be lowercase alnum + _/- "
            f"starting with a letter, up to {MAX_MUTEX_GROUP_LEN} chars"
        )
    return stripped


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
        params=params_from_dict(raw.get("params")),
        cooldown_seconds=_validate_cooldown(raw.get("cooldown_seconds")),
        mutex_group=_validate_mutex_group(raw.get("mutex_group")),
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
    """In-place edit. Accepts ``name``, ``script_path``, ``args``, ``schedule``, ``params``."""
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
    if "params" in fields:
        job.params = params_from_dict(fields["params"])
    if "cooldown_seconds" in fields:
        job.cooldown_seconds = _validate_cooldown(fields["cooldown_seconds"])
    if "mutex_group" in fields:
        job.mutex_group = _validate_mutex_group(fields["mutex_group"])
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
