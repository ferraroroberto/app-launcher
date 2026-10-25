"""Screen time export routes — the sheet's Send to task-os (#1678, step 2/2).

    GET  /api/screen-time/export   {configured, last_ok, last_error}
    POST /api/screen-time/export   send the last 7 completed days now

The numbers themselves come from the vendored ``GET /api/usage/time``; these
two routes only drive :mod:`src.usage.export`, the same code the
``usage-export`` CLI and the nightly Job run. A failed send answers with the
failure kind's status (409 not set up / category missing / no usage log, 503
task-os unreachable, 502 task-os refused) and a message fit for the sheet.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from src.usage import export as usage_export
from src.webapp_config import WebappConfig

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/screen-time")


@router.get("/export")
async def export_status(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    status = await asyncio.to_thread(usage_export.read_status)
    return {"configured": usage_export.is_configured(cfg), **status}


@router.post("/export")
async def export_now(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    try:
        result = await asyncio.to_thread(usage_export.run_export, cfg)
    except usage_export.ExportError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    status = await asyncio.to_thread(usage_export.read_status)
    return {"result": result.to_dict(), "configured": True, **status}
