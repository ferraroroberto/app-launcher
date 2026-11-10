"""Settings › Inspiration: the /inspiration-scan source list and the X login.

``GET /api/inspiration/sources`` -> the source list, a ``revision`` for the
next save, the latest digest's status and whether the file can be edited.
``PUT /api/inspiration/sources`` with ``{revision, likes, newsletter, youtube,
x}`` -> the same payload, after a validated, atomic save (or a 4xx whose
``detail`` says why and leaves the file unchanged).
``GET`` / ``POST /api/inspiration/login-x`` -> whether the X login window is
open, closed or failed; the POST opens it on this PC.

The format and the file belong to fleet-config's inspiration-scan skill
(``src.inspiration_sources``); this router validates and relays. Auth is the
app-level ``BearerTokenMiddleware`` that gates every ``/api/*`` route.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from app.webapp.routers._helpers import maybe_json
from src.inspiration import login as inspiration_login
from src.inspiration import sources as inspiration_sources
from src.webapp_config import WebappConfig

router = APIRouter()


@router.get("/api/inspiration/sources")
async def get_sources(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    return await asyncio.to_thread(inspiration_sources.read_state, cfg.fleet_config_dir)


@router.put("/api/inspiration/sources")
async def put_sources(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    payload = await maybe_json(request)
    try:
        return await asyncio.to_thread(inspiration_sources.save, cfg.fleet_config_dir, payload)
    except inspiration_sources.InspirationError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))


@router.get("/api/inspiration/login-x")
async def get_login_x() -> Dict[str, Any]:
    return await asyncio.to_thread(inspiration_login.status)


@router.post("/api/inspiration/login-x")
async def post_login_x(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    try:
        return await asyncio.to_thread(inspiration_login.start, cfg.fleet_config_dir)
    except inspiration_login.LoginStartError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
