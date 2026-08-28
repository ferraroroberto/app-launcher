"""Shared run-mechanics for the Jobs tab: dry-run modes + real admission+spawn.

Split off ``app/webapp/routers/jobs.py`` (a single-file god-router
candidate flagged by ``/codebase-audit``). This module owns the parts of
the run path that are reused across triggers:

* :func:`_dry_run_check` / :func:`_dry_run_execute` — the two dry-run
  modes (issue #69), used only by the manual ``POST /run`` route.
* :func:`_admit_and_spawn` — the cooldown gate plus the HTTP framing of
  :func:`src.jobs_queue.admit_and_spawn` (the mutex admission itself,
  shared with the chain dispatcher and the executor since issue #794).
  The shared tail of every *real* fire, used by both the manual
  ``POST /run`` route (in ``jobs.py``) and the webhook ``POST /hook``
  route (in ``jobs_webhook_routes.py``) so cooldown and the
  queued-vs-spawned response shape live in exactly one place.

Kept separate from ``jobs_webhook_routes.py`` so neither of the two
route modules needs to import the other (a shared dependency, not a
`jobs.py` <-> `jobs_webhook_routes.py` cycle).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from fastapi import HTTPException

from src import jobs as jobs_mod
from src.jobs_config import Job, JobsConfig

logger = logging.getLogger(__name__)


async def _dry_run_check(job: Job, raw_params: Dict[str, Any]) -> Dict[str, Any]:
    """Dry-run mode 2: resolve the invocation without spawning the child.

    Writes a synthetic ``dry_run_success`` / ``dry_run_failed`` record
    (no ``exit_code``) so history shows "would this even start?" without
    side effects. Deliberately bypasses the executor funnel — nothing is
    ever spawned, so there is no child to track.
    """
    from app.cli.commands.run_job_cmd import build_invocation  # local: avoids cycle

    run_dir = await asyncio.to_thread(
        jobs_mod.new_run_dir, job.id, jobs_mod.new_run_id()
    )
    stamped = datetime.now().isoformat(timespec="seconds")
    meta: Dict[str, Any] = jobs_mod.seed_run_meta(
        job, "manual", stamped, run_id=run_dir.name, finished_at=stamped, dry_run=True
    )
    if raw_params:
        meta["params"] = raw_params
    try:
        argv, _cwd, _env = await asyncio.to_thread(
            build_invocation, job, raw_params, run_dir
        )
        if job.env:
            # "Would this even start?" includes the env overlay (issue
            # #72): an unresolvable $secret: reference must fail the
            # check, not wait for a real fire. Values are discarded —
            # only argv ever lands in the note.
            from src.jobs_secrets import resolve_env_overlay  # local: leaf util
            from src.webapp_config import load_webapp_config

            wcfg = await asyncio.to_thread(load_webapp_config)
            resolve_env_overlay(job.env, wcfg.secrets)
        meta["status"] = "dry_run_success"
        meta["note"] = "resolved: " + " ".join(argv)
    except (OSError, ValueError) as exc:
        meta["status"] = "dry_run_failed"
        meta["note"] = str(exc)
    jobs_mod.write_run_json(run_dir, **meta)
    jobs_mod.invalidate_stats_cache(job.id)
    logger.info(f"🧪 dry-run check {job.id}/{run_dir.name} → {meta['status']}")
    return {
        "run_id": run_dir.name,
        "job_id": job.id,
        "status": meta["status"],
        "dry_run": True,
    }


async def _dry_run_execute(job: Job, raw_params: Dict[str, Any]) -> Dict[str, Any]:
    """Dry-run mode 1: spawn the child with ``JOB_DRY_RUN=1`` set.

    Goes through the real executor (so opted-in scripts see the env var
    and suppress side effects) but skips cooldown + mutex — it is an
    explicit verification fire, not a scheduled/queued run.
    """
    run_dir = await asyncio.to_thread(
        jobs_mod.new_run_dir, job.id, jobs_mod.new_run_id()
    )
    started_at = datetime.now().isoformat(timespec="seconds")
    base_meta: Dict[str, Any] = jobs_mod.seed_run_meta(
        job, "manual", started_at, run_id=run_dir.name, status="pending", dry_run=True
    )
    if raw_params:
        base_meta["params"] = raw_params
    jobs_mod.write_run_json(run_dir, **base_meta)
    try:
        await asyncio.to_thread(
            jobs_mod.spawn_run_job_detached,
            job.id,
            run_dir.name,
            "manual",
            raw_params or None,
            True,
        )
    except OSError as exc:
        jobs_mod.write_run_json(
            run_dir,
            finished_at=datetime.now().isoformat(timespec="seconds"),
            exit_code=-1,
            status="failed",
        )
        raise HTTPException(status_code=500, detail=f"spawn failed: {exc}")
    return {"run_id": run_dir.name, "job_id": job.id, "dry_run": True}


async def _admit_and_spawn(
    job: Job,
    cfg: JobsConfig,
    raw_params: Dict[str, Any],
    trigger: str,
    *,
    extra_run_meta: Optional[Dict[str, Any]] = None,
    on_run_dir: Optional[Callable[[Any], None]] = None,
) -> Dict[str, Any]:
    """Cooldown gate, then the shared mutex admission, in HTTP terms.

    The shared tail of every *real* (non-dry-run) fire — reused by the
    manual ``POST /run`` route (``trigger="manual"``) and the webhook
    ``POST /hook`` route (``trigger="webhook"``, ``extra_run_meta`` carrying
    ``trigger_source``). Two things live here and nowhere else: the
    cooldown gate (a route-only concern — a chain or scheduled fire is an
    explicit consequence, not user mashing) and the translation of the
    admission outcome into a response body or an ``HTTPException``. The
    admission itself is :func:`src.jobs_queue.admit_and_spawn`, shared with
    the chain dispatcher and the executor's scheduled-fire gate (issue
    #794). ``on_run_dir`` fires right after the run directory is created
    (before the queued-vs-spawn branch) so a caller can persist extra files
    (e.g. ``_webhook.json``) alongside ``run.json`` regardless of which
    branch this fire takes.
    """
    # Cooldown admission gate. Runs before we pre-create the run dir so a
    # cooled-down mash-fire produces no on-disk record (the dir would
    # otherwise be orphaned with status=pending). See jobs.cooldown_check
    # for the anchor semantics — skipped records do not extend the window.
    cooldown_state = await asyncio.to_thread(jobs_mod.cooldown_check, job)
    if cooldown_state is not None:
        remaining, cooldown_seconds, _anchor_id = cooldown_state
        raise HTTPException(
            status_code=429,
            detail={
                "detail": "cooldown",
                "retry_after_seconds": remaining,
                "cooldown_seconds": cooldown_seconds,
            },
            headers={"Retry-After": str(remaining)},
        )

    # Mutex-group admission + spawn. Shared with the chain dispatcher and
    # the executor's own scheduled-fire gate (issue #794) — see
    # src.jobs_queue.admit_and_spawn for the queued-vs-spawn contract. Runs
    # as one to_thread hop: every step in it is blocking file/subprocess
    # work, so there is nothing for the event loop to interleave.
    meta = await asyncio.to_thread(
        jobs_mod.admit_and_spawn,
        cfg.jobs,
        job,
        trigger,
        params=raw_params,
        extra_meta=extra_run_meta,
        on_run_dir=on_run_dir,
    )
    if meta is None:
        # Unreachable: spawn_when_free defaults to True, so every route fire
        # produces a record. Fail loud rather than return a bodyless 200.
        raise HTTPException(status_code=500, detail="admission produced no run")

    if meta["status"] == "queued":
        return {
            "run_id": meta["run_id"],
            "job_id": job.id,
            "status": "queued",
            "mutex_group": meta["mutex_group"],
            "mutex_blocked_by": meta["mutex_blocked_by"],
        }
    if meta["status"] == "failed":
        # Spawn failed. admit_and_spawn already recorded the failure on the
        # run so the UI surfaces it instead of a stuck "pending".
        raise HTTPException(
            status_code=500, detail=f"spawn failed: {meta['spawn_error']}"
        )
    return {"run_id": meta["run_id"], "job_id": job.id}
