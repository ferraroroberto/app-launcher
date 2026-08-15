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
* ``once``           — a single one-off run          (``at: "YYYY-MM-DDTHH:MM"``)

This module owns registry *storage* (load/save the JSON file) and CRUD
(add/update/pause/resume/remove). The ``Schedule``/``Param``/``Job``/
``JobsConfig`` dataclasses and their ``*_from_dict``/``validate_*``
helpers live in :mod:`src.jobs_config_models`; the chain-cycle graph
algorithm lives in :mod:`src.jobs_config_chain`. Both are re-exported
here so existing callers (``from src.jobs_config import X``) keep
working unchanged — same facade pattern as the earlier ``src.jobs``
split (issue #315).

``DEFAULT_JOBS_PATH``/``load_jobs``/``save_jobs`` stay defined in *this*
module rather than moving to a storage-only submodule: tests across the
suite monkeypatch ``src.jobs_config.DEFAULT_JOBS_PATH`` (and, in one
case, ``src.jobs_config.load_jobs`` itself) directly, and a monkeypatch
only takes effect on the module that owns the name being patched — see
``src/jobs.py``'s docstring for the same gotcha on the ``src.jobs``
split.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from src._json_io import atomic_write_json
from src.jobs_config_chain import (
    _validate_chain_consistency,
    detect_chain_cycle,
)
from src.jobs_config_models import (
    MAX_COOLDOWN_SECONDS,
    MAX_MUTEX_GROUP_LEN,
    MAX_WATCHDOG_SECONDS,
    PARAM_KINDS,
    SCHEDULE_TYPES,
    WEEKLY_DAYS,
    Job,
    JobsConfig,
    Param,
    Schedule,
    _validate_chain_list,
    _validate_cooldown,
    _validate_max_runtime,
    _validate_mutex_group,
    _validate_no_output,
    env_from_dict,
    job_from_dict,
    kind_config_from_dict,
    make_job_id,
    param_from_dict,
    params_from_dict,
    schedule_from_dict,
    validate_kind_shape,
)
from src.jobs_webhook import webhook_from_dict

__all__ = [
    # models (src.jobs_config_models)
    "SCHEDULE_TYPES",
    "WEEKLY_DAYS",
    "PARAM_KINDS",
    "MAX_COOLDOWN_SECONDS",
    "MAX_MUTEX_GROUP_LEN",
    "MAX_WATCHDOG_SECONDS",
    "Schedule",
    "schedule_from_dict",
    "Param",
    "param_from_dict",
    "params_from_dict",
    "Job",
    "env_from_dict",
    "job_from_dict",
    "make_job_id",
    "kind_config_from_dict",
    "validate_kind_shape",
    "JobsConfig",
    # chain (src.jobs_config_chain)
    "detect_chain_cycle",
    # storage + CRUD (this module)
    "PROJECT_ROOT",
    "DEFAULT_JOBS_PATH",
    "load_jobs",
    "save_jobs",
    "get_by_id",
    "add_job",
    "update_job",
    "pause_job",
    "resume_job",
    "remove_by_id",
]

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JOBS_PATH = PROJECT_ROOT / "config" / "jobs.json"


# ------------------------------------------------------------ load / save


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
    atomic_write_json(target, cfg.to_dict())
    return target


# ----------------------------------------------------------- mutations


def get_by_id(cfg: JobsConfig, job_id: str) -> Optional[Job]:
    return next((j for j in cfg.jobs if j.id == job_id), None)


def add_job(cfg: JobsConfig, job: Job) -> Job:
    """Append ``job`` and persist. Raises ``ValueError`` on duplicate id
    or on chain inconsistency (unknown downstream / cycle) — the cycle
    check sees the post-add registry, so adding the last edge of a cycle
    is rejected before the file is touched.
    """
    if any(j.id == job.id for j in cfg.jobs):
        raise ValueError(f"job id already exists: {job.id}")
    if not job.added_at:
        job.added_at = datetime.now().isoformat(timespec="seconds")
    cfg.jobs.append(job)
    try:
        _validate_chain_consistency(cfg)
    except ValueError:
        cfg.jobs.pop()
        raise
    cfg.jobs.sort(key=lambda j: j.name.lower())
    save_jobs(cfg)
    return job


# Declarative spec for update_job's single-field edits: (attr name, transform
# applied to the raw incoming value, "only set when truthy" flag). Each entry
# drives one `setattr(job, attr, transform(fields[attr]))` in the loop below
# — a new plain field only needs a row here, not another hand-written branch.
# ``kind``/``script_path``/``kind_config`` (validated together as one
# effective post-edit shape) and ``on_success``/``on_failure`` (validated
# together with an atomic revert-on-cycle) don't fit this shape — both are
# genuinely cross-field and stay as their own explicit blocks below.
_SIMPLE_UPDATE_FIELDS: Tuple[Tuple[str, Callable[[Any], Any], bool], ...] = (
    ("name", lambda v: str(v).strip(), True),
    ("args", lambda v: str(v or ""), False),
    ("schedule", schedule_from_dict, False),
    ("params", params_from_dict, False),
    ("cooldown_seconds", _validate_cooldown, False),
    ("max_runtime_seconds", _validate_max_runtime, False),
    ("no_output_seconds", _validate_no_output, False),
    ("mutex_group", _validate_mutex_group, False),
    ("confirm", bool, False),
    ("alert_on_failure", bool, False),
    ("visible", bool, False),
    ("elevated", bool, False),
    ("webhook", webhook_from_dict, False),
    ("env", env_from_dict, False),
)


def update_job(cfg: JobsConfig, job_id: str, **fields: Any) -> Optional[Job]:
    """In-place edit. Accepts every field named in ``_SIMPLE_UPDATE_FIELDS``
    above (the source of truth for plain single-field edits), plus the
    cross-field groups handled explicitly below: ``kind``/``script_path``/
    ``kind_config`` and ``on_success``/``on_failure``.
    """
    job = get_by_id(cfg, job_id)
    if job is None:
        return None

    for attr, transform, only_if_truthy in _SIMPLE_UPDATE_FIELDS:
        if attr not in fields:
            continue
        raw = fields[attr]
        if only_if_truthy and not raw:
            continue
        setattr(job, attr, transform(raw))

    # kind / script_path / kind_config are validated together as the
    # *effective* post-edit shape (issue #70) — an edit to any one of the
    # three must still describe a structurally valid job, and validation
    # runs before any of the three is mutated so a rejected edit leaves the
    # job untouched.
    if "kind" in fields or "script_path" in fields or "kind_config" in fields:
        eff_kind = (
            str(fields["kind"] or "").strip() if "kind" in fields else job.kind
        )
        if "script_path" in fields:
            # Present-but-empty is meaningful here (clearing script_path
            # when switching to inline-shell/http-check), not "no change".
            eff_script_path = str(fields["script_path"] or "").strip()
        else:
            eff_script_path = job.script_path
        eff_kind_config = (
            kind_config_from_dict(fields["kind_config"])
            if "kind_config" in fields
            else job.kind_config
        )
        validate_kind_shape(eff_kind, eff_script_path, eff_kind_config)
        if "kind" in fields:
            job.kind = eff_kind
        if "script_path" in fields:
            # Unlike other fields, an explicit empty string here is
            # meaningful (clearing script_path when switching to
            # inline-shell/http-check) rather than "no change" — so this
            # assigns whenever the key is present, not only when truthy.
            job.script_path = eff_script_path
        if "kind_config" in fields:
            job.kind_config = eff_kind_config

    # Snapshot the chain edges so we can revert atomically on cycle.
    prev_success, prev_failure = job.on_success, job.on_failure
    if "on_success" in fields:
        job.on_success = _validate_chain_list("on_success", fields["on_success"])
    if "on_failure" in fields:
        job.on_failure = _validate_chain_list("on_failure", fields["on_failure"])
    if ("on_success" in fields) or ("on_failure" in fields):
        try:
            _validate_chain_consistency(cfg)
        except ValueError:
            job.on_success, job.on_failure = prev_success, prev_failure
            raise
    cfg.jobs.sort(key=lambda j: j.name.lower())
    save_jobs(cfg)
    return job


def pause_job(cfg: JobsConfig, job_id: str) -> Optional[Job]:
    """Park the live schedule under ``paused_schedule`` and replace the
    active ``schedule`` with ``none`` so the schtasks resync layer
    removes the entries on the next sync. Idempotent — pausing an
    already-paused job is a no-op (the original payload is preserved).
    """
    job = get_by_id(cfg, job_id)
    if job is None:
        return None
    if job.is_paused:
        return job
    if job.schedule.type == "none":
        # Nothing to park — pausing a manual-only job would be a confusing
        # no-op, so reject explicitly so the UI can surface it.
        raise ValueError("cannot pause a job whose schedule is 'none'")
    job.paused_schedule = job.schedule
    job.schedule = Schedule(type="none")
    save_jobs(cfg)
    return job


def resume_job(cfg: JobsConfig, job_id: str) -> Optional[Job]:
    """Restore the parked ``paused_schedule`` onto ``schedule`` and clear
    the parked field. Resuming a job that was never paused is a no-op.
    """
    job = get_by_id(cfg, job_id)
    if job is None:
        return None
    if not job.is_paused or job.paused_schedule is None:
        return job
    job.schedule = job.paused_schedule
    job.paused_schedule = None
    save_jobs(cfg)
    return job


def remove_by_id(cfg: JobsConfig, job_id: str) -> Optional[Job]:
    """Remove the job with ``job_id`` and strip any leftover chain
    references to it from every other job's ``on_success`` /
    ``on_failure``. Cascade-strip is preferred over reject-if-referenced
    because reject would force users into a multi-step delete dance.
    """
    removed: Optional[Job] = None
    for i, job in enumerate(cfg.jobs):
        if job.id == job_id:
            removed = cfg.jobs.pop(i)
            break
    if removed is None:
        return None
    for j in cfg.jobs:
        if job_id in (j.on_success or ()):
            j.on_success = [x for x in j.on_success if x != job_id]
        if job_id in (j.on_failure or ()):
            j.on_failure = [x for x in j.on_failure if x != job_id]
    save_jobs(cfg)
    return removed
