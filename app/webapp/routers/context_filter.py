"""Context-filter control surface (issue #713): the machine-wide off/shadow/
rewrite mode switch, the per-harness applicability matrix, and savings
telemetry from the fleet's PreToolUse context-filter hook
(fleet-config#392/#541/#544).

``GET /api/context-filter`` -> ``{mode, harnesses, stats, age_seconds}``.
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

from app.webapp.demand_cache import DemandCache
from app.webapp.routers._helpers import maybe_json
from src.context_filter_state import (
    HARNESS_SUPPORT,
    VALID_MODES,
    read_mode,
    read_stats,
    write_mode,
)
from src.webapp_config import WebappConfig

router = APIRouter()

# How often the answer is rebuilt while the Settings card is polled.
CONTEXT_FILTER_TICK_S = 5.0


@router.get("/api/context-filter")
async def get_context_filter(request: Request) -> Dict[str, Any]:
    """The mode, the harness matrix and the savings stats.

    The stats parse is the slow part: the telemetry log is re-parsed whenever
    any agent session appends to it, which on a busy box is most polls. So the
    answer is a snapshot (:class:`app.webapp.demand_cache.DemandCache`) that
    the lifespan rebuilds every :data:`CONTEXT_FILTER_TICK_S` while this route
    is polled; ``age_seconds`` is its age and a mode change marks it stale.
    """
    answer, age_seconds = await context_filter_cache(request.app).get()
    return {**answer, "age_seconds": age_seconds}


def context_filter_cache(app: Any) -> DemandCache[Dict[str, Any]]:
    """The app's context-filter snapshot."""
    cache = getattr(app.state, "context_filter_cache", None)
    if cache is None:
        cache = app.state.context_filter_cache = DemandCache(
            "GET /api/context-filter",
            lambda: _build_context_filter(app),
            tick_s=CONTEXT_FILTER_TICK_S,
            max_age_s=3 * CONTEXT_FILTER_TICK_S,
        )
    return cache


async def _build_context_filter(app: Any) -> Dict[str, Any]:
    cfg: WebappConfig = app.state.webapp_config
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
    context_filter_cache(request.app).mark_dirty()
    return {"mode": fresh}
