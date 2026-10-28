"""Coding-tab endpoints that are about a *project row* rather than a session.

Favorites, the launch-flag preview (a small read of webapp_config's `claude`
subtree, surfaced on its own path for the options card), the per-project git
status the tiles colour themselves from, and the row's ``⋯`` project menu —
VS Code, the read-only changes viewer, Explorer (#802, #977) — none of which
spawn or track a PTY session.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Tuple

from fastapi import APIRouter, HTTPException, Request

from src.session_view.git_changes import file_diff, is_git_repo, list_changes, open_folder
from src.scanner import ProjectDir, git_status, scan_project_dirs
from src.launching.vscode_workspace import (
    ensure_workspace_file,
    is_vscode_installed,
    open_workspace,
)
from src.webapp_config import WebappConfig, update_webapp_config

from app.webapp.hot_poll import start_or_join
from app.webapp.routers._helpers import maybe_json

router = APIRouter()
slow_logger = logging.getLogger("launcher.slowreq")


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
    checked in :func:`src.session_view.git_changes.safe_relative_path` before git sees it.
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


# The git-status fan-out's own threads. One git_status per repo on the
# loop's default executor filled it, so every other to_thread caller
# (/api/version, sessions, config writes) queued behind a fleet-wide scan.
# Bounded: a large fleet takes a little longer, and nothing else waits on it.
_GIT_STATUS_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="git-status")


# Overlapping calls share scans, and never a stale one: a call that
# arrives while a scan runs waits for the NEXT scan, which every call from
# that window shares, so each caller sees git state from at or after its own
# arrival. At most two scans per key exist: the running one and one queued.
# (Joining the running scan instead served a scan begun before the caller's
# change: a repo dirtied just before a page load read as clean.) The
# start/settle mechanics behind this are shared with hot_poll.HotPoll's own
# single-flight queuing — see hot_poll.start_or_join, the one copy.
_GIT_STATUS_RUNNING: Dict[Tuple[str, Tuple[str, ...]], "asyncio.Future[Dict[str, Any]]"] = {}
_GIT_STATUS_NEXT: Dict[Tuple[str, Tuple[str, ...]], "asyncio.Future[Dict[str, Any]]"] = {}


# Served from memory while younger than this; the SPA's ~45 s poll and every
# page-load boot read it without spawning git. 120 s is several polls, so a
# phone that keeps polling never meets a blocking scan.
GIT_STATUS_FRESH_S = 10.0
GIT_STATUS_STALE_MAX_S = 120.0
# A scan this slow leaves a breadcrumb in slow-requests.log (the live app's
# INFO logs for app.webapp.* are dropped, that file persists).
_SLOW_SCAN_S = 1.0

_GitStatusCache = Dict[Tuple[str, Tuple[str, ...]], Tuple[float, Dict[str, Any]]]


def _timed_git_status(project_dir: Path) -> Tuple[Any, float]:
    started = time.perf_counter()
    status = git_status(project_dir)
    return status, time.perf_counter() - started


async def _scan_git_status(
    projects_dir: str, ignore: Tuple[str, ...], cache: _GitStatusCache
) -> Dict[str, Any]:
    started_at = time.monotonic()
    projects = scan_project_dirs(Path(projects_dir), list(ignore))
    loop = asyncio.get_running_loop()
    timed = await asyncio.gather(
        *(loop.run_in_executor(_GIT_STATUS_POOL, _timed_git_status, p.project_dir) for p in projects)
    )
    payload = {
        "projects": [
            {"id": p.id, **gs.to_dict()} for p, (gs, _) in zip(projects, timed)
        ]
    }
    # Stamped with the scan's start: it reflects git at or after that moment.
    cache[(projects_dir, ignore)] = (started_at, payload)
    elapsed = time.monotonic() - started_at
    if elapsed >= _SLOW_SCAN_S and projects:
        slowest_s, slowest = max((secs, p.name) for p, (_, secs) in zip(projects, timed))
        slow_logger.info(
            "⏱️ git-status scan: %d repos in %.1fs (git time summed %.1fs, slowest %s %.2fs)",
            len(projects), elapsed, sum(secs for _, secs in timed), slowest, slowest_s,
        )
    return payload


def _git_status_cache(request: Request) -> _GitStatusCache:
    """The app's last scan per key — on app.state so each app instance (each test's) has its own."""
    state = request.app.state
    cache = getattr(state, "git_status_cache", None)
    if cache is None:
        cache = state.git_status_cache = {}
    return cache


def _consume(future: "asyncio.Future[Any]") -> None:
    """Mark a background scan's outcome as retrieved; a failed one keeps the older cache."""
    if not future.cancelled():
        future.exception()


@router.get("/api/claude-code/git-status")
async def claude_git_status(request: Request, fresh: bool = False) -> Dict[str, Any]:
    """Per-project git state for the Coding tiles + Board backlog flags.

    Runs ``git`` once per project (branch + clean/dirty + default
    branch) — fanned out across worker threads. That is ~700 ms of process
    spawns for a fleet of repos, so the last scan is kept in memory: younger
    than :data:`GIT_STATUS_FRESH_S` it is served as is; up to
    :data:`GIT_STATUS_STALE_MAX_S` it is served at once while one scan
    refreshes it behind the response; older (or none yet) the call waits for
    a scan. Every answer carries ``age_seconds``, so the SPA states how old
    the git state it shows is. A commit or branch switch therefore shows by
    the next poll after the refresh it triggered, about one ~45 s poll
    interval; ``?fresh=true`` (the Projects toolbar's refresh) always waits
    for a scan that starts after the call. A call that waits and overlaps a
    running scan waits for the next one, shared with every call from that
    window (#1268); ``shield`` keeps one caller's disconnect from cancelling
    it for the rest.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    key = (str(cfg.projects_dir), tuple(cfg.projects_ignore))
    cache = _git_status_cache(request)
    scan = functools.partial(_scan_git_status, *key, cache)
    cached = None if fresh else cache.get(key)
    if cached is not None:
        age = time.monotonic() - cached[0]
        if age < GIT_STATUS_STALE_MAX_S:
            running = _GIT_STATUS_RUNNING.get(key)
            if age >= GIT_STATUS_FRESH_S and (running is None or running.done()):
                start_or_join(key, _GIT_STATUS_RUNNING, _GIT_STATUS_NEXT, scan).add_done_callback(_consume)
            return {**cached[1], "age_seconds": round(age, 1)}
    future = start_or_join(key, _GIT_STATUS_RUNNING, _GIT_STATUS_NEXT, scan)
    return {**await asyncio.shield(future), "age_seconds": 0.0}
