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
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from src import jobs as jobs_mod
from src.jobs_argv import compose_argv
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
            }
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        add_job(cfg, job)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    # Re-sync the Task Scheduler entries for this job. Best-effort —
    # schtasks failures log a warning but don't undo the registry write.
    await asyncio.to_thread(jobs_mod.sync_schtasks, job)
    return {"job": _decorate_job(job)}


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
    try:
        job = update_job(cfg, job_id, **patch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job {job_id}")
    await asyncio.to_thread(jobs_mod.sync_schtasks, job)
    return {"job": _decorate_job(job)}


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


# ----------------------------------------------------------- run


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
    try:
        compose_argv(job, raw_params)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

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


def _kill_process_tree(pid: int, grace_seconds: float = 5.0) -> List[int]:
    """Terminate ``pid`` and its children; SIGKILL survivors after grace.

    Returns the PIDs that were signalled (best-effort; missing process
    is not an error). All ``psutil`` exceptions are swallowed so the
    route can finalise the run record regardless of how messy the
    process tree turned out to be.
    """
    import psutil  # local — keeps import out of cold start

    signalled: List[int] = []
    try:
        parent = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return signalled
    try:
        children = parent.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        children = []
    procs = [parent] + children
    for p in procs:
        try:
            p.terminate()
            signalled.append(p.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    try:
        _, alive = psutil.wait_procs(procs, timeout=grace_seconds)
    except psutil.Error:
        alive = procs
    for p in alive:
        try:
            p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return signalled


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
        signalled = await asyncio.to_thread(_kill_process_tree, pid)
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
