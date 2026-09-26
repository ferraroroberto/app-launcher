"""Coding-tab endpoints that are about a *project row* rather than a session.

Favorites, the launch-flag preview (a small read of webapp_config's `claude`
subtree, surfaced on its own path for the options card), the per-project git
status the tiles colour themselves from, and the row's ``⋯`` project menu —
VS Code, the read-only changes viewer, Explorer (#802, #977) — none of which
spawn or track a PTY session.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from src.git_changes import file_diff, is_git_repo, list_changes, open_folder
from src.scanner import ProjectDir, git_status, scan_project_dirs
from src.vscode_workspace import (
    ensure_workspace_file,
    is_vscode_installed,
    open_workspace,
)
from src.webapp_config import WebappConfig, update_webapp_config

from app.webapp.routers._helpers import claude_flags_payload, maybe_json

router = APIRouter()


def _project_or_404(request: Request, project_id: str) -> ProjectDir:
    """Resolve a Coding-row project id through the same scan the tiles use.

    The row's actions (VS Code, changes, folder) all take the scanner slug,
    never a path from the browser — the directory comes from the scan.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    projects = scan_project_dirs(Path(cfg.projects_dir), list(cfg.projects_ignore))
    project = next((p for p in projects if p.id == project_id), None)
    if project is None:
        raise HTTPException(status_code=404, detail=f"unknown project: {project_id}")
    return project


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
    project = _project_or_404(request, project_id)
    # Re-checked server-side rather than trusted from the greyed-out button:
    # the SPA's `available` flag is a poll-old snapshot, and the 503 names the
    # real reason instead of surfacing a spawn failure as a generic 500.
    if not is_vscode_installed():
        raise HTTPException(status_code=503, detail="the 'code' CLI is not on PATH")
    try:
        workspace, created = await asyncio.to_thread(
            ensure_workspace_file, Path(cfg.projects_dir), project.name
        )
        pid = await asyncio.to_thread(open_workspace, workspace)
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail=f"could not open VS Code: {exc}"
        ) from exc
    return {"ok": True, "workspace": str(workspace), "created": created, "pid": pid}


@router.get("/api/claude-code/changes/{project_id}")
async def project_changes(project_id: str, request: Request) -> Dict[str, Any]:
    """The working-tree file list behind the row menu's *Show changes* (#977).

    Read-only and on demand — never folded into the 45 s ``git-status``
    poll, whose payload stays a cheap ``dirty`` bool per project. A folder
    that isn't a git repository answers 409 so the SPA can say so inline
    instead of showing an empty list.
    """
    project = _project_or_404(request, project_id)
    if not is_git_repo(project.project_dir):
        raise HTTPException(status_code=409, detail="not a git repository")
    changes = await asyncio.to_thread(list_changes, project.project_dir)
    return {"id": project.id, "name": project.name, **changes.to_dict()}


@router.get("/api/claude-code/changes/{project_id}/diff")
async def project_file_diff(project_id: str, request: Request, path: str = "") -> Dict[str, Any]:
    """One file's unified diff for the accordion viewer (#977).

    ``path`` is a repo-relative path the ``changes`` payload handed out;
    anything else — absolute, ``..``, a symlink out of the tree — is 400,
    checked in :func:`src.git_changes.safe_relative_path` before git sees it.
    """
    project = _project_or_404(request, project_id)
    if not is_git_repo(project.project_dir):
        raise HTTPException(status_code=409, detail="not a git repository")
    try:
        result = await asyncio.to_thread(file_diff, project.project_dir, path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"bad path: {exc}") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/api/claude-code/folder/{project_id}")
async def open_project_folder(project_id: str, request: Request) -> Dict[str, Any]:
    """Open the project directory in Windows Explorer on the PC (#977).

    A local-machine action like the VS Code route above: Explorer opens on
    the launcher host, not the phone. Nothing is tracked afterwards. A host
    without Explorer answers 503 with the reason.
    """
    project = _project_or_404(request, project_id)
    try:
        pid = await asyncio.to_thread(open_folder, project.project_dir)
    except OSError as exc:
        raise HTTPException(status_code=503, detail=f"could not open folder: {exc}") from exc
    return {"ok": True, "path": str(project.project_dir), "pid": pid}


@router.get("/api/claude-code/flags")
async def claude_flags(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    return claude_flags_payload(cfg)


# The git-status fan-out's own threads (#1264). One git_status per repo on the
# loop's default executor filled it, so every other to_thread caller
# (/api/version, sessions, config writes) queued behind a fleet-wide scan.
# Bounded: a large fleet takes a little longer, and nothing else waits on it.
_GIT_STATUS_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="git-status")


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
    loop = asyncio.get_running_loop()
    statuses = await asyncio.gather(
        *(loop.run_in_executor(_GIT_STATUS_POOL, git_status, p.project_dir) for p in projects)
    )
    return {
        "projects": [
            {"id": p.id, **gs.to_dict()} for p, gs in zip(projects, statuses)
        ]
    }
