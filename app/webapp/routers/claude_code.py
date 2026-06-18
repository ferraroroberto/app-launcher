"""Claude-code config-shaped endpoint: launch-flag preview.

A small read of webapp_config (the `claude` subtree of /api/config),
surfaced on its own path for the options card's flag preview.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from src.scanner import git_status, scan_project_dirs
from src.webapp_config import (
    ALWAYS_ON_CLAUDE_FLAGS,
    VALID_CLAUDE_EFFORTS,
    VALID_CLAUDE_MODELS,
    VALID_CLAUDE_PERMISSION_MODES,
    WebappConfig,
    build_claude_flags,
    update_webapp_config,
)

router = APIRouter()


@router.post("/api/claude-code/favorites")
async def toggle_favorite(request: Request) -> Dict[str, Any]:
    """Star/unstar a coding project (issue #250).

    Body: ``{"id": "<scanner-slug>", "favorite": true|false}``. Membership in
    ``coding_favorites`` is set idempotently — favoriting an already-favorite
    (or unfavoriting an absent) id is a no-op that still returns 200 — so a
    double-tap from the phone can't corrupt the list. Persisted to
    webapp_config and mirrored back into ``app.state`` so the next ``/api/apps``
    render reflects it without a reload.
    """
    body = await request.json()
    project_id = str(body.get("id") or "").strip()
    if not project_id:
        raise HTTPException(status_code=400, detail="missing project id")
    favorite = bool(body.get("favorite"))

    cfg: WebappConfig = request.app.state.webapp_config
    # Preserve order, drop dupes — the list is the user's, kept tidy.
    favorites = [f for f in cfg.coding_favorites if f != project_id]
    if favorite:
        favorites.append(project_id)

    new_cfg = update_webapp_config(coding_favorites=favorites)
    request.app.state.webapp_config = new_cfg
    return {"ok": True, "coding_favorites": new_cfg.coding_favorites}


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
