"""Launcher config read/patch + machine status readout.

`/api/status` exposes the tunnel URL, TLS cert presence, and a
terminal-reachability hint so the SPA can explain up front when the
live terminal won't work on the current connection.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request

from src.webapp_config import (
    ALWAYS_ON_CLAUDE_FLAGS,
    VALID_CLAUDE_EFFORTS,
    VALID_CLAUDE_MODELS,
    WebappConfig,
    build_claude_flags,
    update_webapp_config,
)

from app.webapp.middleware import terminal_reachability
from app.webapp.routers._helpers import PROJECT_ROOT, cert_present

router = APIRouter()


@router.get("/api/config")
async def get_config(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    return {
        "host": cfg.host,
        "port": cfg.port,
        "projects_dir": cfg.projects_dir,
        "apps_scan_root": cfg.apps_scan_root,
        "claude": {
            "model": cfg.claude_model,
            "effort": cfg.claude_effort,
            "verbose": cfg.claude_verbose,
            "debug": cfg.claude_debug,
            "models_available": list(VALID_CLAUDE_MODELS),
            "efforts_available": list(VALID_CLAUDE_EFFORTS),
            "always_on_flags": list(ALWAYS_ON_CLAUDE_FLAGS),
            "computed_flags": build_claude_flags(cfg),
        },
        "auth_password_set": bool(cfg.auth_password),
    }


@router.post("/api/config")
async def patch_config(request: Request) -> Dict[str, Any]:
    body = await request.json()
    allowed = {
        "projects_dir",
        "apps_scan_root",
        "claude_model",
        "claude_effort",
        "claude_verbose",
        "claude_debug",
    }
    patch = {k: v for k, v in body.items() if k in allowed}
    try:
        new_cfg = update_webapp_config(**patch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    request.app.state.webapp_config = new_cfg
    return {
        "ok": True,
        "claude_flags": build_claude_flags(new_cfg),
    }


@router.get("/api/status")
async def status(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    tunnel_file = PROJECT_ROOT / "webapp" / "last_tunnel_url.txt"
    tunnel_url: Optional[str] = None
    if tunnel_file.exists():
        try:
            tunnel_url = tunnel_file.read_text(encoding="utf-8").strip() or None
        except OSError:
            tunnel_url = None
    return {
        "projects_dir": cfg.projects_dir,
        "apps_scan_root": cfg.apps_scan_root,
        "tunnel_url": tunnel_url,
        "tls": cert_present(),
        "terminal": terminal_reachability(request),
    }
