"""``/api/jobs`` surface — list, CRUD, run, run-history (issue #47).

The Jobs tab's API mirrors the Apps tab's shape (see
``app/webapp/routers/apps.py``): same bearer-token middleware, same
``maybe_json`` body parsing, same ``HTTPException`` error model.

Trigger funnel: every run — manual (phone tap / Stream Deck via
``POST /api/jobs/<id>/run``) and scheduled (Task Scheduler) — goes
through ``launcher.py run-job <id>``. The route pre-creates the run
directory so it can return the new ``run_id`` immediately, then spawns
the executor detached.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from src import jobs as jobs_mod
from src.diagnostics import kill_process_tree
from src.jobs_argv import compose_argv
from src.jobs_preflight import has_errors, preflight
from src.jobs_config import (
    Job,
    Schedule,
    add_job,
    get_by_id,
    job_from_dict,
    load_jobs,
    make_job_id,
    params_from_dict,
    pause_job,
    remove_by_id,
    resume_job,
    schedule_from_dict,
    update_job,
)

from app.webapp.routers._helpers import maybe_json

logger = logging.getLogger(__name__)
router = APIRouter()


def _truthy(value: Optional[str]) -> bool:
    """Interpret a query-string flag as a boolean (``1``/``true``/``yes``)."""
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _decorate_job(job: Job) -> Dict[str, Any]:
    """API shape for one job — base fields plus runtime decoration.

    ``next_run`` is queried from schtasks (best-effort, ``None`` on
    error or N/A); ``last_run`` is the most recent on-disk run record;
    ``running`` is a quick "is the latest run still in progress" flag;
    ``stats`` carries the p50/p95/success-rate aggregates plus the
    ``last7`` sparkline payload; ``stuck`` flags an over-long running run;
    ``queue_depth`` is the count of pending entries in this job's mutex
    queue (0 when no group).
    """
    payload = job.to_dict()
    # Paused jobs render with a "paused — was X" chip so the user sees
    # both that the schedule isn't ticking AND what it will restore to.
    if job.is_paused and job.paused_schedule is not None:
        was = job.paused_schedule.chip()
        payload["schedule_chip"] = "paused" + (" — was " + was if was else "")
    else:
        payload["schedule_chip"] = job.schedule.chip()
    payload["paused"] = job.is_paused
    payload["target_kind"] = job.target_kind
    payload["next_run"] = jobs_mod.query_next_run(job.id)
    # Computed next fire from the schedule shape (issue #229). Unlike the
    # schtasks string above, this is sortable + countdown-able. None for
    # manual-only / paused / already-elapsed-once jobs.
    nf = jobs_mod.next_fire(job.schedule)
    payload["next_run_epoch"] = int(nf.timestamp()) if nf is not None else None
    payload["next_run_iso"] = (
        nf.isoformat(timespec="seconds") if nf is not None else None
    )
    latest = jobs_mod.latest_run(job.id)
    if latest is not None:
        payload["last_run"] = {
            "run_id": latest.get("run_id"),
            "status": latest.get("status"),
            "started_at": latest.get("started_at"),
            "finished_at": latest.get("finished_at"),
            "exit_code": latest.get("exit_code"),
            "trigger": latest.get("trigger"),
            "duration_seconds": latest.get("duration_seconds"),
        }
    else:
        payload["last_run"] = None
    payload["running"] = jobs_mod.is_running(job.id)
    payload["stats"] = jobs_mod.run_stats(job.id)
    payload["stuck"] = jobs_mod.is_stuck(job.id)
    payload["queue_depth"] = (
        len(jobs_mod.peek_mutex_queue(job.mutex_group)) if job.mutex_group else 0
    )
    return payload


def _preflight_gate(job: Job, *, acknowledged: bool) -> List[Dict[str, str]]:
    """Run save-time pre-flight (issue #69) and enforce the two-phase flow.

    * Any **error** raises ``HTTPException`` 400 carrying a structured
      ``problems`` list — the save is blocked regardless of acknowledgement.
    * **Warnings** without ``acknowledged`` raise ``_PreflightWarnings`` so
      the route can short-circuit with a ``saved: false`` body, keeping the
      dialog open with a "Save anyway" button.
    * **Warnings** *with* ``acknowledged`` (or no problems) return the
      warning dicts so the route can surface them in the success response.
    """
    problems = preflight(job)
    dicts = [p.to_dict() for p in problems]
    if has_errors(problems):
        raise HTTPException(
            status_code=400,
            detail={"reason": "preflight", "problems": dicts},
        )
    if dicts and not acknowledged:
        raise _PreflightWarnings(dicts)
    return dicts


class _PreflightWarnings(Exception):
    """Internal signal: warnings-only pre-flight that wasn't acknowledged."""

    def __init__(self, warnings: List[Dict[str, str]]) -> None:
        super().__init__("preflight warnings")
        self.warnings = warnings




# ----------------------------------------------------------- CRUD


@router.get("/api/jobs")
async def get_jobs(request: Request) -> Dict[str, Any]:
    cfg = load_jobs()
    # query_next_run shells out to schtasks per job — offload the whole
    # decoration to a worker thread so the event loop doesn't block.
    decorated = await asyncio.to_thread(
        lambda: [_decorate_job(j) for j in cfg.jobs]
    )
    return {"jobs": decorated}


@router.get("/api/jobs/agenda")
async def get_jobs_agenda(request: Request, days: int = 7) -> Dict[str, Any]:
    """Upcoming scheduled fires over the next ``days`` (issue #230).

    Backs the Jobs-tab agenda panel. Expands each non-paused job's schedule
    across ``[now, now+days)`` into a flat, time-sorted occurrence list (the
    client groups it by day), plus a ``frequent`` summary for the dense
    minutes/hourly jobs that would flood the window. Lightweight — no
    schtasks, no per-job decoration — but still offloaded to a worker thread
    to keep the event loop clear. ``days`` is clamped to 1..14.
    """
    days = max(1, min(14, days))
    cfg = load_jobs()
    now = datetime.now()
    end = now + timedelta(days=days)

    def _build() -> Dict[str, Any]:
        occurrences: List[Dict[str, Any]] = []
        frequent: List[Dict[str, Any]] = []
        for job in cfg.jobs:
            if job.is_paused or job.schedule.type == "none":
                continue
            cadence = job.schedule.chip()
            if job.schedule.type in jobs_mod.FREQUENT_SCHEDULE_TYPES:
                frequent.append(
                    {"job_id": job.id, "name": job.name, "cadence": cadence}
                )
                continue
            for fire in jobs_mod.upcoming_fires(job.schedule, start=now, end=end):
                occurrences.append(
                    {
                        "job_id": job.id,
                        "name": job.name,
                        "fire_epoch": int(fire.timestamp()),
                        "fire_iso": fire.isoformat(timespec="minutes"),
                        "cadence": cadence,
                    }
                )
        occurrences.sort(key=lambda o: o["fire_epoch"])
        return {
            "days": days,
            "generated_epoch": int(now.timestamp()),
            "occurrences": occurrences,
            "frequent": frequent,
        }

    return await asyncio.to_thread(_build)


@router.post("/api/jobs")
async def create_job(request: Request) -> Dict[str, Any]:
    body = await maybe_json(request)
    name = str(body.get("name") or "").strip()
    script_path = str(body.get("script_path") or "").strip()
    args = str(body.get("args") or "")
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if not script_path:
        raise HTTPException(status_code=400, detail="script_path is required")
    try:
        schedule = schedule_from_dict(body.get("schedule"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Validate params (issue #67) at the boundary so a bad shape fails
    # fast with a 400 instead of being caught downstream by job_from_dict.
    params_raw = body.get("params")
    try:
        params_from_dict(params_raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    cfg = load_jobs()
    job_id = str(body.get("id") or "").strip() or make_job_id(
        name, existing_ids=[j.id for j in cfg.jobs]
    )
    try:
        job = job_from_dict(
            {
                "id": job_id,
                "name": name,
                "script_path": script_path,
                "args": args,
                "schedule": schedule.to_dict(),
                "added_at": datetime.now().isoformat(timespec="seconds"),
                "params": params_raw or [],
                "cooldown_seconds": body.get("cooldown_seconds"),
                "mutex_group": body.get("mutex_group"),
                "on_success": body.get("on_success"),
                "on_failure": body.get("on_failure"),
                "confirm": body.get("confirm"),
                "visible": body.get("visible"),
            }
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Save-time pre-flight (issue #69). Errors 400 with a structured
    # problems list; warnings short-circuit to a saved:false body unless
    # the caller already acknowledged them.
    acknowledged = bool(body.get("acknowledge_warnings"))
    try:
        warnings = _preflight_gate(job, acknowledged=acknowledged)
    except _PreflightWarnings as warn:
        return {"saved": False, "warnings": warn.warnings}

    try:
        add_job(cfg, job)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    # Re-sync the Task Scheduler entries for this job. Best-effort —
    # schtasks failures log a warning but don't undo the registry write.
    await asyncio.to_thread(jobs_mod.sync_schtasks, job)
    return {"job": _decorate_job(job), "saved": True, "warnings": warnings}


@router.put("/api/jobs/{job_id}")
async def edit_job(job_id: str, request: Request) -> Dict[str, Any]:
    body = await maybe_json(request)
    cfg = load_jobs()
    existing = get_by_id(cfg, job_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"unknown job {job_id}")
    patch: Dict[str, Any] = {}
    if "name" in body:
        patch["name"] = body["name"]
    if "script_path" in body:
        patch["script_path"] = body["script_path"]
    if "args" in body:
        patch["args"] = body["args"]
    if "schedule" in body:
        patch["schedule"] = body["schedule"]
    if "params" in body:
        patch["params"] = body["params"]
    if "cooldown_seconds" in body:
        patch["cooldown_seconds"] = body["cooldown_seconds"]
    if "mutex_group" in body:
        patch["mutex_group"] = body["mutex_group"]
    if "on_success" in body:
        patch["on_success"] = body["on_success"]
    if "on_failure" in body:
        patch["on_failure"] = body["on_failure"]
    if "confirm" in body:
        patch["confirm"] = body["confirm"]
    if "visible" in body:
        patch["visible"] = body["visible"]

    # Save-time pre-flight (issue #69) on the *effective* post-edit job.
    # Pre-flight only inspects script_path + args, so synthesize a
    # candidate from the existing job overlaid with this patch and gate on
    # it before update_job persists. Suffix is validated here (mirroring
    # update_job) so a .txt edit fails with its own clear message rather
    # than a misleading "script not found" from pre-flight.
    eff_script = (
        str(patch["script_path"]).strip()
        if patch.get("script_path")
        else existing.script_path
    )
    if "args" in patch:
        eff_args = str(patch["args"] or "")
    else:
        eff_args = existing.args
    if Path(eff_script).suffix.lower() not in (".py", ".bat"):
        raise HTTPException(
            status_code=400,
            detail=f"script_path must end .py or .bat, got {eff_script!r}",
        )
    candidate = replace(existing, script_path=eff_script, args=eff_args)
    acknowledged = bool(body.get("acknowledge_warnings"))
    try:
        warnings = _preflight_gate(candidate, acknowledged=acknowledged)
    except _PreflightWarnings as warn:
        return {"saved": False, "warnings": warn.warnings}

    try:
        job = update_job(cfg, job_id, **patch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job {job_id}")
    await asyncio.to_thread(jobs_mod.sync_schtasks, job)
    return {"job": _decorate_job(job), "saved": True, "warnings": warnings}


@router.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str) -> Dict[str, Any]:
    cfg = load_jobs()
    removed = remove_by_id(cfg, job_id)
    if removed is None:
        raise HTTPException(status_code=404, detail=f"unknown job {job_id}")
    await asyncio.to_thread(jobs_mod.delete_schtasks, job_id)
    return {"removed": removed.id}


# ----------------------------------------------------------- pause / resume


@router.post("/api/jobs/{job_id}/pause")
async def pause(job_id: str) -> Dict[str, Any]:
    """Park the live schedule under ``paused_schedule`` and resync
    schtasks (which removes the entries for this job).
    """
    cfg = load_jobs()
    try:
        job = pause_job(cfg, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job {job_id}")
    # Schedule is now ``none`` → sync_schtasks deletes the entries.
    await asyncio.to_thread(jobs_mod.sync_schtasks, job)
    return {"job": _decorate_job(job)}


@router.post("/api/jobs/{job_id}/resume")
async def resume(job_id: str) -> Dict[str, Any]:
    """Restore the parked schedule and resync schtasks."""
    cfg = load_jobs()
    job = resume_job(cfg, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job {job_id}")
    await asyncio.to_thread(jobs_mod.sync_schtasks, job)
    return {"job": _decorate_job(job)}


# ----------------------------------------------------------- run / dry-run


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
    meta: Dict[str, Any] = dict(
        run_id=run_dir.name,
        job_id=job.id,
        name=job.name,
        trigger="manual",
        script_path=job.script_path,
        args=job.args,
        started_at=stamped,
        finished_at=stamped,
        dry_run=True,
    )
    if raw_params:
        meta["params"] = raw_params
    try:
        argv, _cwd, _env = await asyncio.to_thread(
            build_invocation, job, raw_params
        )
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
    base_meta: Dict[str, Any] = dict(
        run_id=run_dir.name,
        job_id=job.id,
        name=job.name,
        trigger="manual",
        script_path=job.script_path,
        args=job.args,
        started_at=started_at,
        status="pending",
        dry_run=True,
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


@router.post("/api/jobs/{job_id}/run")
async def run_job(job_id: str, request: Request) -> Dict[str, Any]:
    cfg = load_jobs()
    job = get_by_id(cfg, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job {job_id}")

    # Typed parameter payload (issue #67). Empty body keeps today's
    # one-tap fire path for parameter-less jobs. Validation happens up
    # front via compose_argv so bad values never get a run directory.
    body = await maybe_json(request)
    raw_params = body.get("params") if isinstance(body, dict) else None
    if raw_params is None:
        raw_params = {}
    if not isinstance(raw_params, dict):
        raise HTTPException(
            status_code=400, detail="params must be an object"
        )

    # Dry-run modes (issue #69). "check" verifies the job would *start*
    # (path/venv/param resolution) without spawning the child; "execute"
    # spawns with JOB_DRY_RUN=1 so opted-in scripts suppress side effects.
    # Both are explicit verification fires, so they bypass cooldown +
    # mutex (pressing 🧪 should never be answered with "cooled down").
    dry_mode = body.get("dry_run") if isinstance(body, dict) else None
    if dry_mode not in (None, "execute", "check"):
        raise HTTPException(
            status_code=400, detail="dry_run must be 'execute' or 'check'"
        )

    # Confirm-on-fire gate (issue #69). A job flagged ``confirm`` must
    # carry ``?confirmed=1`` to actually execute — this keeps the gate
    # honest against a direct curl / Stream Deck hit, not just the UI.
    # A dry-run "check" has no side effects, so it is exempt.
    if job.confirm and dry_mode != "check" and not _truthy(
        request.query_params.get("confirmed")
    ):
        raise HTTPException(status_code=403, detail="confirmation required")

    if dry_mode == "check":
        return await _dry_run_check(job, raw_params)

    try:
        compose_argv(job, raw_params)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if dry_mode == "execute":
        return await _dry_run_execute(job, raw_params)

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

    # Mutex-group admission. If another job in the same group is running
    # or pending, this fire is QUEUED (not rejected — that's cooldown's
    # job). We still pre-create the run dir so the caller gets a real
    # run_id back; the executor that finalises the in-flight head will
    # pop this entry from the queue and spawn it detached. See
    # src.jobs.drain_mutex_queue for the spawn-time guard.
    holder = await asyncio.to_thread(
        jobs_mod.mutex_collision, cfg.jobs, job
    )

    run_dir = await asyncio.to_thread(
        jobs_mod.new_run_dir, job.id, jobs_mod.new_run_id()
    )
    started_at = datetime.now().isoformat(timespec="seconds")
    base_meta: Dict[str, Any] = dict(
        run_id=run_dir.name,
        job_id=job.id,
        name=job.name,
        trigger="manual",
        script_path=job.script_path,
        args=job.args,
        started_at=started_at,
    )
    if raw_params:
        base_meta["params"] = raw_params

    if holder is not None:
        # Queue it. status=queued does not feed stats / streaks; the
        # finalising executor of the holder job will flip it to running.
        base_meta["status"] = "queued"
        base_meta["mutex_group"] = job.mutex_group
        base_meta["mutex_blocked_by"] = holder.id
        jobs_mod.write_run_json(run_dir, **base_meta)
        await asyncio.to_thread(
            jobs_mod.enqueue_mutex,
            job.mutex_group,
            {
                "job_id": job.id,
                "run_id": run_dir.name,
                "trigger": "manual",
                "params": raw_params or None,
            },
        )
        logger.info(
            f"🪢 queued {job.id}/{run_dir.name} behind {holder.id} "
            f"(mutex_group={job.mutex_group!r})"
        )
        return {
            "run_id": run_dir.name,
            "job_id": job.id,
            "status": "queued",
            "mutex_group": job.mutex_group,
            "mutex_blocked_by": holder.id,
        }

    base_meta["status"] = "pending"
    jobs_mod.write_run_json(run_dir, **base_meta)
    try:
        await asyncio.to_thread(
            jobs_mod.spawn_run_job_detached,
            job.id,
            run_dir.name,
            "manual",
            raw_params or None,
        )
    except OSError as exc:
        # Spawn failed → record the failure on the run we just created
        # so the UI surfaces it instead of a stuck "pending".
        jobs_mod.write_run_json(
            run_dir,
            finished_at=datetime.now().isoformat(timespec="seconds"),
            exit_code=-1,
            status="failed",
        )
        raise HTTPException(status_code=500, detail=f"spawn failed: {exc}")
    return {"run_id": run_dir.name, "job_id": job.id}


# ----------------------------------------------------------- kill stuck run


@router.post("/api/jobs/{job_id}/runs/{run_id}/kill")
async def kill_job_run(job_id: str, run_id: str) -> Dict[str, Any]:
    cfg = load_jobs()
    job = get_by_id(cfg, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job {job_id}")
    run_dir = jobs_mod.runs_dir(job_id) / run_id
    if not run_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"unknown run {run_id}")
    record = await asyncio.to_thread(jobs_mod.read_run, run_dir)
    status = record.get("status")
    if status not in {"running", "pending"}:
        raise HTTPException(
            status_code=409,
            detail=f"run is {status!r}, not killable",
        )
    pid = record.get("pid")
    signalled: List[int] = []
    if isinstance(pid, int) and pid > 0:
        signalled = await asyncio.to_thread(kill_process_tree, pid, 5.0)
    finished_at = datetime.now().isoformat(timespec="seconds")
    started_at = record.get("started_at")
    duration: Optional[float] = None
    if isinstance(started_at, str):
        try:
            d = datetime.fromisoformat(finished_at) - datetime.fromisoformat(
                started_at
            )
            duration = d.total_seconds()
        except ValueError:
            duration = None
    await asyncio.to_thread(
        jobs_mod.write_run_json,
        run_dir,
        status="failed",
        exit_code=-9,
        finished_at=finished_at,
        duration_seconds=duration,
        killed=True,
    )
    jobs_mod.invalidate_stats_cache(job_id)
    # If the killed run was the head of a mutex group, drain so the
    # queue doesn't wedge waiting for a finalisation that already
    # happened (the executor we just killed will not run its own
    # finalisation block).
    if job.mutex_group:
        await asyncio.to_thread(jobs_mod.drain_mutex_queue, job.mutex_group)
    logger.info(
        f"🛑 killed stuck run {job_id}/{run_id} "
        f"pid={pid!r} signalled={signalled}"
    )
    return {"run_id": run_id, "job_id": job_id, "signalled": signalled}


# ----------------------------------------------------------- run history


@router.get("/api/jobs/{job_id}/runs")
async def get_job_runs(job_id: str) -> Dict[str, Any]:
    cfg = load_jobs()
    if get_by_id(cfg, job_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown job {job_id}")
    runs = await asyncio.to_thread(jobs_mod.list_runs, job_id)
    return {"runs": runs}


@router.get("/api/jobs/{job_id}/runs/{run_id}")
async def get_job_run(job_id: str, run_id: str) -> Dict[str, Any]:
    cfg = load_jobs()
    if get_by_id(cfg, job_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown job {job_id}")
    run_dir = jobs_mod.runs_dir(job_id) / run_id
    if not run_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"unknown run {run_id}")
    record = await asyncio.to_thread(jobs_mod.read_run, run_dir)
    record.setdefault("run_id", run_id)
    record["output_tail"] = await asyncio.to_thread(
        jobs_mod.read_output_tail, run_dir
    )
    return {"run": record}
