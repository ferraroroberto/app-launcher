"""Claude-code config-shaped endpoints: flag preview + bat/workspace sync.

Flags is a small read of webapp_config (the `claude` subtree of /api/config).
Generate previews and applies the workspace ↔ remote.bat sync — driven
entirely by config (projects_dir + computed flags).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Request

from src.bat_generator import (
    discover_orphan_bats,
    discover_workspaces,
    run_generate,
)
from src.webapp_config import (
    ALWAYS_ON_CLAUDE_FLAGS,
    VALID_CLAUDE_EFFORTS,
    VALID_CLAUDE_MODELS,
    WebappConfig,
    build_claude_flags,
)

from app.webapp.routers._helpers import maybe_json

router = APIRouter()


@router.get("/api/claude-code/flags")
async def claude_flags(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    return {
        "model": cfg.claude_model,
        "effort": cfg.claude_effort,
        "verbose": cfg.claude_verbose,
        "debug": cfg.claude_debug,
        "models_available": list(VALID_CLAUDE_MODELS),
        "efforts_available": list(VALID_CLAUDE_EFFORTS),
        "always_on_flags": list(ALWAYS_ON_CLAUDE_FLAGS),
        "computed_flags": build_claude_flags(cfg),
    }


@router.get("/api/claude-code/generate")
async def claude_generate_preview(request: Request) -> Dict[str, Any]:
    """Preview what `POST /api/claude-code/generate` would do."""
    cfg: WebappConfig = request.app.state.webapp_config
    projects_dir = Path(cfg.projects_dir)
    return {
        "projects_dir": str(projects_dir),
        "workspaces": [
            {
                "name": w.name,
                "project_dir": str(w.project_dir),
                "bat_name": w.bat_name,
                "bat_exists": w.bat_exists,
            }
            for w in discover_workspaces(projects_dir)
        ],
        "orphans": [
            {
                "name": o.name,
                "project_dir": str(o.project_dir),
                "bat_name": o.bat_name,
                "ws_name": o.ws_name,
            }
            for o in discover_orphan_bats(projects_dir)
        ],
    }


@router.post("/api/claude-code/generate")
async def claude_generate(request: Request) -> Dict[str, Any]:
    body = await maybe_json(request)
    cfg: WebappConfig = request.app.state.webapp_config
    overwrite = set(body.get("overwrite") or [])
    create_ws = set(body.get("create_ws") or [])
    result = run_generate(
        projects_dir=Path(cfg.projects_dir),
        flags=build_claude_flags(cfg),
        overwrite_names=overwrite,
        create_ws_names=create_ws,
    )
    return {
        "created": result.created,
        "overwritten": result.overwritten,
        "ws_created": result.ws_created,
        "errors": result.errors,
    }
