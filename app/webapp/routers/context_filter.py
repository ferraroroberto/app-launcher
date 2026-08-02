"""Context-filter control surface (issue #713): the machine-wide off/shadow/
rewrite mode switch, the per-harness applicability matrix, and savings
telemetry from the fleet's PreToolUse context-filter hook
(fleet-config#392/#541/#544).

``GET /api/context-filter`` -> ``{mode, harnesses, stats}``.
``PUT /api/context-filter/mode`` -> validate, write, return the fresh mode.

Route decorators mirror ``routers/config.py`` exactly — no extra auth
dependency; the app-level ``BearerTokenMiddleware`` already gates every
``/api/*`` route the same way every sibling router relies on.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from src.context_filter_state import (
    HARNESS_SUPPORT,
    VALID_MODES,
    read_mode,
    read_stats,
    write_mode,
)
from src.webapp_config import WebappConfig

from app.webapp.routers._helpers import maybe_json

router = APIRouter()


@router.get("/api/context-filter")
async def get_context_filter(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    mode_path = Path(cfg.context_filter_mode_file)
    log_path = Path(cfg.context_filter_log_file)
    # Both reads are blocking file IO — off the event loop, run concurrently
    # since neither depends on the other.
    mode, stats = await asyncio.gather(
        asyncio.to_thread(read_mode, mode_path),
        asyncio.to_thread(read_stats, log_path),
    )
    return {"mode": mode, "harnesses": HARNESS_SUPPORT, "stats": stats}


@router.put("/api/context-filter/mode")
async def put_context_filter_mode(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    body = await maybe_json(request)
    mode = body.get("mode")
    if mode not in VALID_MODES:
        raise HTTPException(
            status_code=400, detail=f"mode must be one of {VALID_MODES}"
        )
    mode_path = Path(cfg.context_filter_mode_file)
    try:
        fresh = await asyncio.to_thread(write_mode, mode_path, mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"mode": fresh}
