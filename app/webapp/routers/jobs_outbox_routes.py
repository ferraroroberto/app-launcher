"""Jobs tab: files a scheduled Job run sends to the owner, no hosted session needed.

``POST /api/jobs/{job_id}/runs/{run_id}/outbox`` — ``{"path", "caption"?}``.
**Loopback-only**, like the session outbox: the caller is the Job's own child
process on this PC, addressing its run through the ``APP_LAUNCHER_JOB_ID`` /
``APP_LAUNCHER_JOB_RUN_ID`` the executor stamps on it. Only a run that is
still ``running`` takes a file, so a stale id cannot add to an old run.

The file goes through :func:`src.session_view.outbox.store` — the session
outbox's guards, caps, refusal reasons and entry shape — into
``<run_dir>/outbox/``, so it is pruned with the run. Roots: the run's own
``JOB_ARTIFACT_DIR`` plus the declared ``outbox_scratch_roots``; a relative
path is taken against the artifact folder.

``GET .../outbox`` lists a run's entries and ``GET .../outbox/{entry_id}``
serves one entry's bytes with the session outbox's headers; both sit behind
the Tailscale + passkey gate (``middleware._TERMINAL_GUARD_RULES``). The
Jobs tab lists them on the run's history row.

Bodies, paths and captions are never logged; a refusal logs its reason.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

from app.webapp.middleware import is_pc_itself
from app.webapp.routers.session_outbox_routes import file_headers, refusal_error
from src import jobs as jobs_mod
from src.jobs.config import get_by_id, load_jobs
from src.session_view import outbox as session_outbox
from src.webapp_config import WebappConfig

logger = logging.getLogger(__name__)

router = APIRouter()


def _refuse(status: int, reason: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"reason": reason, "message": message})


def _run_dir(job_id: str, run_id: str) -> Path:
    """The run's folder, or the refusal naming which half is missing."""
    if not session_outbox.is_safe_id(job_id) or not session_outbox.is_safe_id(run_id):
        raise _refuse(400, "bad_request", "not a job or run id")
    if get_by_id(load_jobs(), job_id) is None:
        raise _refuse(404, "job_not_found", "no job with that id")
    run_dir = jobs_mod.runs_dir(job_id) / run_id
    if not run_dir.is_dir():
        raise _refuse(404, "run_not_found", "no run with that id for this job")
    return run_dir


def run_outbox_listing(run_dir: Path) -> Dict[str, Any]:
    """``{"rev", "entries"}`` a run's Job sent, oldest first."""
    return session_outbox.folder_listing(run_dir / "outbox")


@router.post("/api/jobs/{job_id}/runs/{run_id}/outbox")
async def post_job_outbox(job_id: str, run_id: str, request: Request) -> Dict[str, Any]:
    """Send a file from a running Job to the owner (loopback only)."""
    client_host = request.client.host if request.client else ""
    if not is_pc_itself(client_host, request.headers):
        logger.info("ℹ️ job outbox %s refused: not loopback", job_id)
        raise _refuse(403, "loopback_only", "the outbox takes files from this PC only")
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("path"), str):
        raise _refuse(400, "bad_request", 'expected JSON {"path": "...", "caption": "..."}')
    run_dir = await asyncio.to_thread(_run_dir, job_id, run_id)
    record = await asyncio.to_thread(jobs_mod.read_run, run_dir)
    if record.get("status") != "running":
        logger.info("ℹ️ job outbox %s/%s refused: run_not_running", job_id, run_id)
        raise _refuse(409, "run_not_running", "that run has finished; only a running Job can send a file")
    cfg: WebappConfig = request.app.state.webapp_config
    artifact_dir = str(run_dir / "artifacts")
    roots = session_outbox.allowed_roots(artifact_dir, cfg.outbox_scratch_roots)
    try:
        entry = await asyncio.to_thread(
            session_outbox.store, run_dir / "outbox", body["path"], body.get("caption"), artifact_dir, roots,
        )
    except session_outbox.OutboxRefusal as exc:
        logger.info("ℹ️ job outbox %s/%s refused: %s", job_id, run_id, exc.reason)
        raise refusal_error(exc) from None
    logger.info(
        "ℹ️ job outbox %s/%s: %s %d bytes (%s)", job_id, run_id, entry["kind"], entry["size"], entry["id"][:8],
    )
    return {"ok": True, "job_id": job_id, "run_id": run_id, "entry": entry}


@router.get("/api/jobs/{job_id}/runs/{run_id}/outbox")
async def list_job_outbox(job_id: str, run_id: str) -> Dict[str, Any]:
    """A run's sent files, oldest first (Tailscale + passkey)."""
    run_dir = await asyncio.to_thread(_run_dir, job_id, run_id)
    found = await asyncio.to_thread(run_outbox_listing, run_dir)
    return {"job_id": job_id, "run_id": run_id, **found}


@router.get("/api/jobs/{job_id}/runs/{run_id}/outbox/{entry_id}")
async def job_outbox_file(job_id: str, run_id: str, entry_id: str) -> Response:
    """One sent file's bytes (Tailscale + passkey)."""
    run_dir = await asyncio.to_thread(_run_dir, job_id, run_id)
    found: Optional[Any] = await asyncio.to_thread(
        session_outbox.folder_entry_file, run_dir / "outbox", entry_id,
    )
    if found is None:
        raise _refuse(404, "entry_not_found", "no such file")
    stored, entry = found
    media = str(entry.get("media") or "")
    if media not in session_outbox.SERVED_MEDIA:
        raise _refuse(404, "entry_not_found", "no such file")
    return FileResponse(
        Path(stored), media_type=media,
        headers=file_headers(str(entry.get("name") or "file"), media),
    )
