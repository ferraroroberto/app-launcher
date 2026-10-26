"""Screen time routes — the sheet's Send to task-os (#1678, step 2/2) and its
By feature counts.

    GET  /api/screen-time/export   {configured, last_ok, last_error}
    POST /api/screen-time/export   send the last 7 completed days now
    GET  /api/usage/features       uses per input feature over the last N days

The minutes come from the vendored ``GET /api/usage/time``; the two export
routes only drive :mod:`src.usage.export`, the same code the ``usage-export``
CLI and the nightly Job run. The counts come from :mod:`src.usage.feature_counts`. A failed send answers with the
failure kind's status (409 not set up / category missing / no usage log, 503
task-os unreachable, 502 task-os refused) and a message fit for the sheet.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query, Request

from src.usage import export as usage_export
from src.usage.feature_counts import feature_counts
from src.usage_log import MAX_TIME_DAYS, local_window, open_read_only
from src.webapp_config import WebappConfig

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/screen-time/export")
async def export_status(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    status = await asyncio.to_thread(usage_export.read_status)
    return {"configured": usage_export.is_configured(cfg), **status}


@router.post("/api/screen-time/export")
async def export_now(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    try:
        result = await asyncio.to_thread(usage_export.run_export, cfg)
    except usage_export.ExportError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    status = await asyncio.to_thread(usage_export.read_status)
    return {"result": result.to_dict(), "configured": True, **status}


def _read_feature_counts(days: int) -> Dict[str, Any]:
    since, until = local_window(days)
    db_path = usage_export.default_db_path()
    exists = db_path.is_file()
    if not exists:
        # "No data yet" stays distinct from zero use, as in /api/usage/time.
        conn = sqlite3.connect(":memory:")
        conn.executescript("CREATE TABLE events (ts TEXT, kind TEXT, control_id TEXT, device_class TEXT)")
    else:
        conn = open_read_only(db_path)
    try:
        report = feature_counts(conn, since, until)
    finally:
        conn.close()
    return {**report, "days": days, "log_exists": exists}


@router.get("/api/usage/features")
async def feature_use(days: int = Query(7, ge=1, le=MAX_TIME_DAYS)) -> Dict[str, Any]:
    try:
        return await asyncio.to_thread(_read_feature_counts, days)
    except sqlite3.Error as exc:
        logger.info("ℹ️ usage log: feature counts unavailable (%s)", exc)
        raise HTTPException(status_code=503, detail="usage log unavailable")
