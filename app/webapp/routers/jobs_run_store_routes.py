"""Artifacts, pinning, and live output for Jobs-tab run records (issue #71)."""

from __future__ import annotations

import asyncio
import hmac
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse
from starlette.websockets import WebSocketDisconnect

from app.webapp.middleware import LOOPBACK_HOSTS
from app.webapp.routers._helpers import maybe_json
from src import jobs as jobs_mod
from src.jobs_config import get_by_id, load_jobs

router = APIRouter()


def _known_run_dir(job_id: str, run_id: str) -> Optional[Path]:
    """Return a registered job's existing run dir, else ``None``."""
    if get_by_id(load_jobs(), job_id) is None:
        return None
    run_dir = jobs_mod.runs_dir(job_id) / run_id
    return run_dir if run_dir.is_dir() else None


@router.put("/api/jobs/{job_id}/runs/{run_id}")
async def update_job_run(job_id: str, run_id: str, request: Request) -> dict:
    """Update the keep-forever flag on one canonical run record."""
    run_dir = _known_run_dir(job_id, run_id)
    if run_dir is None:
        raise HTTPException(status_code=404, detail="unknown job or run")
    body = await maybe_json(request)
    if set(body) != {"pinned"} or not isinstance(body.get("pinned"), bool):
        raise HTTPException(status_code=400, detail="body must be {pinned: bool}")
    await asyncio.to_thread(jobs_mod.write_run_json, run_dir, pinned=body["pinned"])
    record = await asyncio.to_thread(jobs_mod.read_run, run_dir)
    record.setdefault("run_id", run_id)
    return {"run": record}


@router.get("/api/jobs/{job_id}/runs/{run_id}/artifacts/{filename:path}")
async def download_job_artifact(job_id: str, run_id: str, filename: str) -> FileResponse:
    """Serve one artifact while keeping resolution strictly inside its run."""
    run_dir = _known_run_dir(job_id, run_id)
    if run_dir is None:
        raise HTTPException(status_code=404, detail="unknown job or run")
    base = (run_dir / "artifacts").resolve()
    candidate = (base / filename).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid artifact path") from exc
    # The API contract is a filename, not an arbitrary nested path. Keeping
    # direct children only makes the jail obvious and matches list_artifacts.
    if candidate.parent != base:
        raise HTTPException(status_code=400, detail="invalid artifact path")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    return FileResponse(candidate, filename=candidate.name)


@router.websocket("/api/jobs/{job_id}/runs/{run_id}/stream")
async def stream_job_run(websocket: WebSocket, job_id: str, run_id: str) -> None:
    """Send one output snapshot, incremental chunks, then final status."""
    await websocket.accept()
    cfg = websocket.app.state.webapp_config
    client_host = websocket.client.host if websocket.client else ""
    if client_host not in LOOPBACK_HOSTS:
        expected = (cfg.auth_token or "").strip()
        if expected:
            presented = websocket.query_params.get("token", "").strip()
            if not (presented and hmac.compare_digest(presented, expected)):
                await websocket.close(code=4401, reason="missing or invalid bearer token")
                return
    run_dir = _known_run_dir(job_id, run_id)
    if run_dir is None:
        await websocket.close(code=4404, reason="unknown job or run")
        return

    output_path = run_dir / "output.log"
    offset = 0
    first = True
    live_statuses = {"queued", "pending", "running"}
    try:
        while True:
            record = await asyncio.to_thread(jobs_mod.read_run, run_dir)
            status = str(record.get("status") or "pending")
            try:
                size = output_path.stat().st_size if output_path.is_file() else 0
            except OSError:
                size = 0
            if first or size < offset:
                snapshot = await asyncio.to_thread(jobs_mod.read_output_tail, run_dir)
                await websocket.send_json(
                    {"type": "snapshot", "output": snapshot, "status": status}
                )
                offset = size
                first = False
            elif size > offset:
                length = size - offset

                def _read_delta() -> bytes:
                    with output_path.open("rb") as handle:
                        handle.seek(offset)
                        return handle.read(length)

                delta = await asyncio.to_thread(_read_delta)
                offset = size
                if delta:
                    await websocket.send_json(
                        {
                            "type": "chunk",
                            "output": delta.decode("utf-8", errors="replace"),
                            "status": status,
                        }
                    )
            if status not in live_statuses:
                await websocket.send_json({"type": "status", "status": status})
                await websocket.close(code=1000)
                return
            await asyncio.sleep(0.1)
    except (OSError, RuntimeError, WebSocketDisconnect):
        return
