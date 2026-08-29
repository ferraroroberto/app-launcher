"""Coding-tab endpoints that are about a *project row* rather than a session.

Favorites, the launch-flag preview (a small read of webapp_config's `claude`
subtree, surfaced on its own path for the options card), the per-project git
status the tiles colour themselves from, and the VS Code open button — none
of which spawn or track a PTY session.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from src.scanner import git_status, scan_project_dirs
from src.vscode_workspace import (
    ensure_workspace_file,
    is_vscode_installed,
    open_workspace,
)
from src.webapp_config import WebappConfig, update_webapp_config

from app.webapp.routers._helpers import claude_flags_payload, maybe_json

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
    body = await maybe_json(request)
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


@router.post("/api/claude-code/vscode/{project_id}")
async def open_project_in_vscode(project_id: str, request: Request) -> Dict[str, Any]:
    """Open a Coding project in the local VS Code (issue #802).

    Resolves the project's sibling ``<name>.code-workspace`` under
    ``projects_dir``, creating it with a minimal one-folder shape when it
    doesn't exist yet, then hands it to the ``code`` CLI. Nothing is tracked
    afterwards — VS Code is its own top-level app, not a launcher-managed
    session, so there is no PID to stop and no Running-sessions row.

    A **local-machine** action, like every other Coding-tab launch: the editor
    opens on the PC the launcher runs on, not on the phone that tapped it.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    projects_dir = Path(cfg.projects_dir)
    projects = scan_project_dirs(projects_dir, list(cfg.projects_ignore))
    project = next((p for p in projects if p.id == project_id), None)
    if project is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {project_id}")
    # Re-checked server-side rather than trusted from the greyed-out button:
    # the SPA's `available` flag is a poll-old snapshot, and the 503 names the
    # real reason instead of surfacing a spawn failure as a generic 500.
    if not is_vscode_installed():
        raise HTTPException(status_code=503, detail="the 'code' CLI is not on PATH")
    try:
        workspace, created = await asyncio.to_thread(
            ensure_workspace_file, projects_dir, project.name
        )
        pid = await asyncio.to_thread(open_workspace, workspace)
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail=f"could not open VS Code: {exc}"
        ) from exc
    return {"ok": True, "workspace": str(workspace), "created": created, "pid": pid}


@router.get("/api/claude-code/flags")
async def claude_flags(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    return claude_flags_payload(cfg)


@router.get("/api/claude-code/git-status")
async def claude_git_status(request: Request) -> Dict[str, Any]:
    """Per-project git state for the Coding tiles + Board backlog flags.

    Runs ``git`` once per project (branch + clean/dirty + default
    branch) — fanned out across worker threads so a fleet of repos
    resolves in well under a second. Always-on since #496 (reversing
    #115's tap-only contract): the SPA calls this once at boot and on a
    slow (~45 s) poll while the Coding or Board tab is visible in a
    foreground page, plus a fresh fetch when the header status button
    opens the off-main popover (#139).
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
