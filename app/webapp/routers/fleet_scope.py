"""Fleet scope control surface: which repos the fleet-wide runs include.

``GET /api/fleet/scope`` -> ``{available, reason, repos: [{name, status}]}``.
``PUT /api/fleet/scope/{repo}`` with ``{status: "active"|"dormant"}`` -> the
fresh listing plus ``outcome`` (``changed`` / ``unchanged``).

The edit is fleet-config's own writer (``src.fleet_scope``); this router only
validates and relays. Auth is the app-level ``BearerTokenMiddleware`` that
gates every ``/api/*`` route, as for the sibling routers.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from src import fleet_scope
from src.webapp_config import WebappConfig

from app.webapp.routers._helpers import maybe_json

router = APIRouter()


@router.get("/api/fleet/scope")
async def get_fleet_scope(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    return await asyncio.to_thread(fleet_scope.list_repos, cfg.fleet_config_dir)


@router.put("/api/fleet/scope/{repo}")
async def put_fleet_scope(repo: str, request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    body = await maybe_json(request)
    status = body.get("status")
    if status not in fleet_scope.VALID_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of {list(fleet_scope.VALID_STATUSES)}",
        )
    listing = await asyncio.to_thread(fleet_scope.list_repos, cfg.fleet_config_dir)
    if not listing["available"]:
        raise HTTPException(status_code=503, detail=listing["reason"])
    if repo not in {r["name"] for r in listing["repos"]}:
        raise HTTPException(status_code=404, detail=f"unknown repo {repo!r}")
    try:
        outcome = await asyncio.to_thread(
            fleet_scope.set_status, cfg.fleet_config_dir, repo, status
        )
    except fleet_scope.FleetScopeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    fresh = await asyncio.to_thread(fleet_scope.list_repos, cfg.fleet_config_dir)
    return {**fresh, "outcome": outcome}
