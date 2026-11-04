"""``GET /api/progress``: the fleet's GitHub totals and token history, cache first.

``?bucket=day|week|month&from=YYYY-MM-DD&to=YYYY-MM-DD`` (default: the last 30
days by day) answers from the saved totals and the hub's cached history at
once; ``refresh=true`` starts a recalculation of both in the background and
the answer says ``refreshing`` while it runs. The maths is
:mod:`src.progress.history`.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from src.progress import buckets, history
from src.webapp_config import WebappConfig

router = APIRouter()


@router.get("/api/progress")
async def get_progress(
    request: Request,
    bucket: str = Query("day"),
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None),
    refresh: bool = Query(False),
) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    if bucket not in buckets.BUCKETS:
        raise HTTPException(status_code=422, detail=f"bucket must be one of {list(buckets.BUCKETS)}")
    try:
        first, last = buckets.parse_range(from_, to)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return await asyncio.to_thread(
        history.read,
        bucket=bucket, first=first, last=last, hub_url=cfg.llm_hub_url,
        fleet_config_dir=cfg.fleet_config_dir, projects_dir=cfg.projects_dir, refresh=refresh,
    )
