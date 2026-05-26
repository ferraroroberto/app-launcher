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
from src.jobs_config import (
    Job,
    Schedule,
    add_job,
    get_by_id,
    job_from_dict,
    load_jobs,
    make_job_id,
    remove_by_id,
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
    ``last7`` sparkline payload; ``stuck`` flags an over-long running run.
    """
    payload = job.to_dict()
    payload["schedule_chip"] = job.schedule.chip()
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


# ----------------------------------------------------------- run


@router.post("/api/jobs/{job_id}/run")
async def run_job(job_id: str, request: Request) -> Dict[str, Any]:
    cfg = load_jobs()
    job = get_by_id(cfg, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job {job_id}")
    # Pre-create the run dir so the caller knows the run id before the
    # detached executor has a chance to write its first byte. The
    # executor reuses this dir via --run-id.
    run_dir = await asyncio.to_thread(
        jobs_mod.new_run_dir, job.id, jobs_mod.new_run_id()
    )
    started_at = datetime.now().isoformat(timespec="seconds")
    jobs_mod.write_run_json(
        run_dir,
        run_id=run_dir.name,
        job_id=job.id,
        name=job.name,
        trigger="manual",
        script_path=job.script_path,
        args=job.args,
        started_at=started_at,
        status="pending",
    )
    try:
        await asyncio.to_thread(
            jobs_mod.spawn_run_job_detached, job.id, run_dir.name, "manual"
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
