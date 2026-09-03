"""``/api/jobs`` surface — CRUD, agenda, pause/resume, run, kill, run-history
(issue #47).

The Jobs tab's API mirrors the Apps tab's shape (see
``app/webapp/routers/apps.py``): same bearer-token middleware, same
``maybe_json`` body parsing, same ``HTTPException`` error model.

Trigger funnel: every run — manual (phone tap / Stream Deck via
``POST /api/jobs/<id>/run``), webhook (an external service via
``POST /api/jobs/<id>/hook``, issue #73), and scheduled (Task Scheduler) —
goes through ``launcher.py run-job <id>``. The route pre-creates the run
directory so it can return the new ``run_id`` immediately, then spawns
the executor detached.

Split off a single-file god-router (``/codebase-audit``): the webhook
route lives in :mod:`app.webapp.routers.jobs_webhook_routes` (mounted
here via ``include_router`` so ``app/webapp/server.py`` still registers
one ``jobs.router``), and the two dry-run modes plus the shared
cooldown+mutex admission/spawn tail live in
:mod:`app.webapp.routers.jobs_run` (imported by both this module and
the webhook one, so neither route module depends on the other). Artifact
downloads, pin updates, and the live-output WebSocket live in
:mod:`app.webapp.routers.jobs_run_store_routes`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from src import jobs as jobs_mod
from src import jobs_index
from src.diagnostics import kill_process_tree
from src.jobs_argv import compose_argv
from src.jobs_preflight import has_errors, preflight
from src.jobs_config import (
    Job,
    add_job,
    get_by_id,
    job_from_dict,
    kind_config_from_dict,
    load_jobs,
    make_job_id,
    params_from_dict,
    pause_job,
    remove_by_id,
    resume_job,
    schedule_from_dict,
    update_job,
    validate_kind_shape,
)

from app.webapp.routers._helpers import maybe_json
from app.webapp.routers.jobs_run import _admit_and_spawn, _dry_run_check, _dry_run_execute
from app.webapp.routers import jobs_run_store_routes, jobs_webhook_routes

logger = logging.getLogger(__name__)
router = APIRouter()
# Webhook trigger route lives in its own module (see the module docstring);
# mounted here so callers of ``app/webapp/server.py`` still see one router.
router.include_router(jobs_webhook_routes.router)
router.include_router(jobs_run_store_routes.router)

# Job fields that are plain optional passthroughs on both create and edit —
# ``create_job`` reads each via ``body.get(field)``, ``edit_job`` reads each
# via ``if field in body: patch[field] = body[field]``. Kept as one list so
# adding a field to the schema can't silently land on only one of the two
# routes (issue #405). ``name``/``script_path``/``args``/``schedule``/``params``
# are excluded — they get real validation/parsing on create that isn't a bare
# passthrough, so they stay hand-written in each route.
JOB_OPTIONAL_FIELDS = (
    "cooldown_seconds",
    "max_runtime_seconds",
    "no_output_seconds",
    "mutex_group",
    "on_success",
    "on_failure",
    "confirm",
    "alert_on_failure",
    "visible",
    "elevated",
    "session_less",
    "kind",
    "kind_config",
    "webhook",
    "env",
)


def _truthy(value: Optional[str]) -> bool:
    """Interpret a query-string flag as a boolean (``1``/``true``/``yes``)."""
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _decorate_job(
    job: Job, run_counts: Optional[tuple[int, int]] = None
) -> Dict[str, Any]:
    """API shape for one job — base fields plus runtime decoration.

    ``next_run`` is queried from schtasks (best-effort, ``None`` on
    error or N/A); ``last_run`` is the most recent on-disk run record;
    ``running`` is a quick "is the latest run still in progress" flag;
    ``stats`` carries the p50/p95/success-rate aggregates plus the
    ``last7`` sparkline payload; ``stuck`` flags an over-long running run;
    ``queue_depth`` is the count of pending entries in this job's mutex
    queue (0 when no group); ``coverage`` flags a schedule that isn't
    firing at all (issue #697) — a missing/disabled Task Scheduler entry or
    an elapsed slot with no run record, read from one cached scan per poll.
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
    # An elevated job is launched by an externally-created /RL HIGHEST task.
    # This non-elevated webapp can show its history and validate its target,
    # but cannot safely run it now or change the external task's state.
    payload["manual_run_allowed"] = not job.elevated
    # A session-less job's entry is externally managed too (issue #757), so
    # pause/resume/schedule edits here would not reach Task Scheduler — the
    # controls are withheld rather than allowed to lie. Manual "run now" is
    # *not* withheld: that spawns the executor in this session directly and
    # never touches the scheduled entry's principal.
    payload["schedule_controls_allowed"] = not (job.elevated or job.session_less)
    # The elevated PowerShell that registers (or re-registers) this job's
    # session-less entries — the one thing this process cannot do itself.
    # None for every other job, so a normal row's payload is unchanged.
    payload["registration_command"] = (
        jobs_mod.registration_script(job) if job.session_less else None
    )
    payload["next_run"] = jobs_mod.query_next_run(job.id)
    # Computed next fire from the schedule shape (issue #229). Unlike the
    # schtasks string above, this is sortable + countdown-able. None for
    # manual-only / paused / already-elapsed-once jobs.
    nf = jobs_mod.next_fire(job.schedule)
    payload["next_run_epoch"] = int(nf.timestamp()) if nf is not None else None
    payload["next_run_iso"] = (
        nf.isoformat(timespec="seconds") if nf is not None else None
    )
    # A run stranded "running" by a dead executor (issue #591) is reconciled
    # opportunistically here, on every poll, before it's decorated for the
    # client — mirrors src.app_runtime.prune_dead's lazy-on-read pattern.
    jobs_mod.reap_stranded_runs(job)
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
    payload["running"] = bool(latest and latest.get("status") == "running")
    payload["stats"] = jobs_mod.run_stats(job.id)
    payload["stuck"] = jobs_mod.is_stuck(job.id, latest=latest)
    # Missed-fire coverage (issue #697). Reads a process-local cached scan —
    # the schtasks half rides the same 30 s bulk-query cache `next_run` uses,
    # so this adds no shell-out to the poll path.
    payload["coverage"] = jobs_mod.coverage_for_job(job.id)
    payload["queue_depth"] = (
        len(jobs_mod.peek_mutex_queue(job.mutex_group)) if job.mutex_group else 0
    )
    payload["run_count"], payload["pinned_count"] = (
        run_counts if run_counts is not None else jobs_index.run_counts(job.id)
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
    # The SQLite mirror is derived. Deleting it and reloading the tab rebuilds
    # it transparently before the first decorated row queries its counts.
    await asyncio.to_thread(jobs_index.ensure_index)
    counts = await asyncio.to_thread(jobs_index.run_counts_by_job)
    # query_next_run shells out to schtasks per job — offload the whole
    # decoration to a worker thread so the event loop doesn't block.
    decorated = await asyncio.to_thread(
        lambda: [_decorate_job(j, counts.get(j.id, (0, 0))) for j in cfg.jobs]
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
    # script_path requiredness is kind-dependent (issue #70: inline-shell /
    # http-check carry no script_path at all) — job_from_dict's
    # validate_kind_shape is the single place that owns this now, so it is
    # deliberately not hard-checked here.
    script_path = str(body.get("script_path") or "").strip()
    args = str(body.get("args") or "")
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
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
                **{field: body.get(field) for field in JOB_OPTIONAL_FIELDS},
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
    for field in ("name", "script_path", "args", "schedule", "params"):
        if field in body:
            patch[field] = body[field]
    for field in JOB_OPTIONAL_FIELDS:
        if field in body:
            patch[field] = body[field]

    # Save-time pre-flight (issue #69) on the *effective* post-edit job.
    # Synthesize a candidate from the existing job overlaid with this patch
    # and gate on it before update_job persists. kind/script_path/kind_config
    # are validated together here (mirroring update_job's own effective-shape
    # check, issue #70) so a bad combination fails with its own clear message
    # rather than a misleading "script not found" from pre-flight.
    eff_kind = str(patch["kind"] or "").strip() if "kind" in patch else existing.kind
    if "script_path" in patch:
        eff_script = str(patch["script_path"] or "").strip()
    else:
        eff_script = existing.script_path
    if "args" in patch:
        eff_args = str(patch["args"] or "")
    else:
        eff_args = existing.args
    try:
        eff_kind_config = (
            kind_config_from_dict(patch["kind_config"])
            if "kind_config" in patch
            else existing.kind_config
        )
        validate_kind_shape(eff_kind, eff_script, eff_kind_config)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    candidate = replace(
        existing,
        kind=eff_kind,
        script_path=eff_script,
        args=eff_args,
        kind_config=eff_kind_config,
    )
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
    existing = get_by_id(cfg, job_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"unknown job {job_id}")
    # elevated (#352) and session_less (#757) are both externally managed:
    # sync_schtasks does not touch their entries, so parking the schedule
    # here would leave Task Scheduler firing on the old one.
    if existing.elevated or existing.session_less:
        raise HTTPException(
            status_code=409,
            detail="schedule is externally managed and cannot be paused here",
        )
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
    existing = get_by_id(cfg, job_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"unknown job {job_id}")
    if existing.elevated or existing.session_less:
        raise HTTPException(
            status_code=409,
            detail="schedule is externally managed and cannot be resumed here",
        )
    job = resume_job(cfg, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job {job_id}")
    await asyncio.to_thread(jobs_mod.sync_schtasks, job)
    return {"job": _decorate_job(job)}


# ----------------------------------------------------------- run / dry-run
# The two dry-run modes and the shared cooldown+mutex admission/spawn tail
# live in app.webapp.routers.jobs_run (see module docstring).


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
    if job.elevated and dry_mode != "check":
        raise HTTPException(
            status_code=409,
            detail=(
                "manual runs are unavailable for externally scheduled jobs"
            ),
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

    # Run-record provenance (issue #72): who fired this, from where, with
    # what credential. token_id/label are set by the auth middleware only
    # for a minted (scoped) token — never the secret itself; the legacy
    # auth_token and loopback callers record ip+ua alone.
    provenance: Dict[str, Any] = {
        "trigger_source": "api",
        "trigger_ip": request.client.host if request.client else "",
        "trigger_ua": request.headers.get("user-agent", ""),
    }
    token_id = getattr(request.state, "token_id", None)
    if token_id:
        provenance["trigger_token_id"] = token_id
        token_label = getattr(request.state, "token_label", "")
        if token_label:
            provenance["trigger_token_label"] = token_label

    return await _admit_and_spawn(
        job, cfg, raw_params, "manual", extra_run_meta=provenance
    )


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


@router.get("/api/jobs/runs/search")
async def search_job_runs(
    request: Request,
    q: str,
    job: Optional[str] = None,
    status: Optional[str] = None,
    since: Optional[str] = None,
) -> Dict[str, Any]:
    """Search indexed output across runs, newest matching run first."""
    query = q.strip()
    if not query:
        raise HTTPException(status_code=400, detail="q is required")
    if job and get_by_id(load_jobs(), job) is None:
        raise HTTPException(status_code=404, detail=f"unknown job {job}")
    if since:
        try:
            datetime.fromisoformat(since)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="since must be ISO-8601") from exc
    matches = await asyncio.to_thread(
        jobs_index.search_runs,
        query,
        job_id=job,
        status=status,
        since=since,
    )
    return {"matches": matches}


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
    # Raw webhook payload (issue #73), when this run was webhook-triggered —
    # surfaced alongside the output tail for the run's expanded panel.
    record["webhook_payload"] = await asyncio.to_thread(
        jobs_mod.read_webhook_payload, run_dir
    )
    record["artifacts"] = await asyncio.to_thread(jobs_mod.list_artifacts, run_dir)
    return {"run": record}
