"""Claude-code config-shaped endpoint: launch-flag preview.

A small read of webapp_config (the `claude` subtree of /api/config),
surfaced on its own path for the options card's flag preview.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Request

from src.scanner import git_status, scan_project_dirs
from src.webapp_config import (
    ALWAYS_ON_CLAUDE_FLAGS,
    VALID_CLAUDE_EFFORTS,
    VALID_CLAUDE_MODELS,
    VALID_CLAUDE_PERMISSION_MODES,
    WebappConfig,
    build_claude_flags,
)

router = APIRouter()


@router.get("/api/claude-code/flags")
async def claude_flags(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    return {
        "model": cfg.claude_model,
        "effort": cfg.claude_effort,
        "verbose": cfg.claude_verbose,
        "debug": cfg.claude_debug,
        "permission_mode": cfg.claude_permission_mode,
        "models_available": list(VALID_CLAUDE_MODELS),
        "efforts_available": list(VALID_CLAUDE_EFFORTS),
        "permission_modes_available": list(VALID_CLAUDE_PERMISSION_MODES),
        "always_on_flags": list(ALWAYS_ON_CLAUDE_FLAGS),
        "computed_flags": build_claude_flags(cfg),
    }


@router.get("/api/claude-code/git-status")
async def claude_git_status(request: Request) -> Dict[str, Any]:
    """Per-project git state for the Coding tab's on-demand flags.

    Runs ``git`` once per project (branch + clean/dirty + default
    branch) — fanned out across worker threads so a fleet of repos
    resolves in well under a second. The SPA calls this only when the
    user taps the check button; nothing here runs on render or poll.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    projects = scan_project_dirs(Path(cfg.projects_dir), list(cfg.projects_ignore))
    statuses = await asyncio.gather(
        *(asyncio.to_thread(git_status, p.project_dir) for p in projects)
    )
    return {
        "projects": [
            {"id": p.id, **gs.to_dict()} for p, gs in zip(projects, statuses)
        ]
    }
