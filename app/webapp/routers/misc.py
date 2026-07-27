"""Catch-all routes: index, healthz, port probing + kill.

Port probe / kill live here because they're not about the app registry —
they're a generic "what's listening on this machine" diagnostic. The
listener→app label mapping uses the registry but doesn't mutate it.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from src import session_client
from src.agents import detect_agents
from src.build_info import build_identity, resolve_git_sha
from src.diagnostics import find_pids_on_port, kill_pids, list_app_listeners
from src.registry import load_registry
from src.scanner import pretty_folder_name
from src.session_host_paths import declared_session_host_paths, paths_touched_between
from src.static_versioning import asset_hash_for, rewrite_index_html
from src.webapp_config import WebappConfig

from app.webapp.routers._helpers import PROJECT_ROOT, STATIC_DIR

_log = logging.getLogger(__name__)

router = APIRouter()

_IDENTITY = build_identity()
_GIT_SHA = _IDENTITY["git_sha"]
_BUILT_AT = _IDENTITY["captured_at"]
_CLAUDE_MD_PATH = PROJECT_ROOT / "CLAUDE.md"


@router.get("/")
async def index(request: Request) -> HTMLResponse:
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=500, detail="index.html missing")
    asset_hashes = getattr(request.app.state, "asset_hashes", {}) or {}
    body = index_path.read_text(encoding="utf-8")
    stamped = rewrite_index_html(body, asset_hashes)
    # Force Safari (iPhone PWA especially) to revalidate the HTML on every
    # load. Without this, a stale cached index.html keeps pointing at a
    # `?v=<old hash>` script that no longer exists after a refactor — the
    # page renders the static skeleton but no JS runs. The HTML body is
    # tiny (~9 KB) so the round-trip cost is negligible.
    return HTMLResponse(
        content=stamped,
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@router.get("/spike/voice-loop")
async def spike_voice_loop(request: Request) -> HTMLResponse:
    """De-risking spike (#246): a hands-free voice-loop prototype.

    Served through the same ``rewrite_index_html`` + no-cache path as ``/`` so
    its module script picks up the asset hash (and never serves stale across
    builds). Bearer-gated like every page (``?token=`` accepted); the page
    bootstraps the passkey terminal token itself.

    Throwaway by design, but **retained for now** (issue #258): the viability
    gate is answered, and the kanban/board view has since shipped (#164,
    completed by #302 — including the board's dictation mics via the shared
    ``voice.js``), so the retention now rests **solely on the orchestrator
    (#245) voice mode**: this loop remains the live reference for wiring real
    narration + a conversation-mode entry point. Delete the set only once
    #245's voice mode has shipped — see ``docs/voice-loop-spike.md`` for the
    retention decision.
    """
    page = STATIC_DIR / "spike-voice-loop.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="voice-loop spike page missing")
    asset_hashes = getattr(request.app.state, "asset_hashes", {}) or {}
    stamped = rewrite_index_html(page.read_text(encoding="utf-8"), asset_hashes)
    return HTMLResponse(
        content=stamped,
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@router.get("/api/version")
async def version(request: Request) -> Dict[str, Any]:
    """Build identity: this webapp process's own (stable, cached at module
    load) plus a live staleness check of the session-host on ``:8446``
    (#615).

    The session-host is deliberately excluded from ``tray.bat --restart``'s
    reclaim sweep (project-scaffolding#35, to protect live PTYs), so it can
    keep running code that is days old with nothing else surfacing that —
    exactly what happened to #611's fix on 2026-07-27. ``session_host.stale``
    compares the session-host's own loaded ``git_sha`` (captured once, at
    *its* process start) against ``head_sha`` (this repo's current ``HEAD``,
    resolved fresh on every call — the webapp's own cached ``git_sha`` isn't
    used for the comparison, since the webapp itself could equally be stale).
    ``session_host.stale`` is a raw fact — it flips ``true`` after *any* merge
    anywhere in the repo, even one that never touched the paths the
    session-host actually loads (#635). ``session_host.stale_relevant`` scopes
    that: ``true`` only when a declared session-host path (``CLAUDE.md``'s
    ``## session-host`` block) was touched between the loaded sha and
    ``head_sha`` — this is the field that should actually drive a restart
    decision. ``session_host: {"reachable": false}`` when the session-host
    can't be reached at all; both ``stale`` and ``stale_relevant`` stay
    ``None`` (never a confident answer) whenever either sha, or the scoped
    diff itself, can't be resolved — e.g. a non-repo test env or an unknown
    host sha.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    asset_hashes = getattr(request.app.state, "asset_hashes", {}) or {}
    head_sha, host_identity = await asyncio.gather(
        asyncio.to_thread(resolve_git_sha),
        asyncio.to_thread(session_client.identity, cfg.session_host_port),
    )
    session_host = _session_host_freshness(host_identity, head_sha)
    return {
        "git_sha": _GIT_SHA,
        "built_at": _BUILT_AT,
        "asset_hash": asset_hash_for(asset_hashes, "styles.css") or "",
        "head_sha": head_sha,
        "session_host": session_host,
    }


def _session_host_freshness(
    identity: Optional[Dict[str, Any]], head_sha: str
) -> Dict[str, Any]:
    """``{"reachable", "git_sha", "started_at", "stale", "stale_relevant"}``
    (#615, scoped by #635) from the session-host's own ``/healthz`` body and
    the repo's current ``head_sha``.

    ``stale`` is ``None`` (unknown, not "not stale") whenever either SHA is
    unresolvable — an unreachable host or a failed ``git`` lookup must never
    read as a false "up to date". ``stale_relevant`` narrows ``stale`` to
    whether a declared session-host path was actually touched between the two
    shas: ``False`` when the shas match (nothing stale, so nothing relevant),
    and ``None`` when ``stale`` itself is unknown, or the scoped diff can't be
    resolved (e.g. the host sha isn't in local git history) — never a
    confident "unaffected" when the comparison couldn't actually run.
    """
    if identity is None:
        return {
            "reachable": False, "git_sha": None, "started_at": None,
            "stale": None, "stale_relevant": None,
        }
    host_sha = identity.get("git_sha")
    stale: Optional[bool] = None
    stale_relevant: Optional[bool] = None
    if host_sha and host_sha != "unknown" and head_sha and head_sha != "unknown":
        stale = host_sha != head_sha
        stale_relevant = False if not stale else _session_host_path_relevance(host_sha, head_sha)
    return {
        "reachable": True,
        "git_sha": host_sha,
        "started_at": identity.get("started_at"),
        "stale": stale,
        "stale_relevant": stale_relevant,
    }


def _session_host_path_relevance(host_sha: str, head_sha: str) -> Optional[bool]:
    """Whether the diff between ``host_sha`` and ``head_sha`` touched a
    declared session-host path (``CLAUDE.md``'s ``## session-host`` block).

    ``None`` when the declaration can't be parsed or the diff can't be
    resolved — see :func:`src.session_host_paths.paths_touched_between`.
    """
    paths = declared_session_host_paths(_CLAUDE_MD_PATH)
    if not paths:
        return None
    return paths_touched_between(PROJECT_ROOT, host_sha, head_sha, paths)


@router.get("/api/terminal-themes")
async def terminal_themes() -> Dict[str, Any]:
    """User-tunable xterm theme overrides (issue #381), VS Code-style.

    Reads the machine-local ``webapp/terminal-themes.json`` — per-mode
    xterm theme keys plus an optional ``minimumContrastRatio`` knob, e.g.
    ``{"light": {"background": "#fbf5e9", "minimumContrastRatio": 5}}`` —
    which terminal.js deep-merges over its built-in palettes at boot.
    Missing or invalid file → empty overrides, never an error (the
    built-ins are always a complete theme). See
    ``webapp/terminal-themes.sample.json`` for the shape.
    """
    path = PROJECT_ROOT / "webapp" / "terminal-themes.json"
    if not path.exists():
        return {"themes": {}}
    try:
        import json as _json

        data = _json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("terminal-themes.json must be a JSON object")
        return {"themes": data}
    except (OSError, ValueError) as exc:
        _log.warning("⚠️ terminal-themes.json unreadable — ignored: %s", exc)
        return {"themes": {}}


@router.get("/api/agents")
async def agents() -> Dict[str, Any]:
    """Coding agents the launcher can spawn, each with a live PATH check.

    The Coding tab uses ``available`` to disable an agent's per-tile
    launch button (with a hover hint) when its CLI isn't installed.
    """
    return {"agents": detect_agents()}


@router.get("/healthz")
async def healthz() -> Dict[str, Any]:
    return {"ok": True, "service": "launcher"}


@router.get("/api/ports/probe")
async def probe_ports() -> Dict[str, Any]:
    """Discover every LISTEN socket owned by a python/streamlit process.

    Streamlit auto-increments its port past 8501, so a fixed port
    list misses apps — this enumerates listeners dynamically. Each
    listener is labelled with the app it belongs to (matched on the
    process's working directory) so you know what you're killing.
    """
    dir_names = _registered_dir_names()
    owners = list_app_listeners()
    pid_to_port = {o.pid: o.port for o in owners}
    out = [
        {
            "port": owner.port,
            "pid": owner.pid,
            "name": owner.name,
            "exe": owner.exe,
            "cmdline": owner.cmdline_str(),
            "app": _app_label_for_dir(owner.cwd, dir_names),
            # When this listener is a helper service the UI nests it under
            # the parent app's row instead of duplicating the app name.
            "parent_port": pid_to_port.get(owner.parent_pid) if owner.parent_pid else None,
            "service": _service_label(owner.cmdline),
        }
        for owner in owners
    ]
    return {"listeners": out}


@router.post("/api/ports/{port}/kill")
async def kill_port(port: int) -> Dict[str, Any]:
    if port < 1 or port > 65535:
        raise HTTPException(status_code=400, detail="port out of range")
    pids = find_pids_on_port(port)
    if not pids:
        return {"port": port, "killed": [], "detail": "nothing was listening"}
    killed, errors = kill_pids(pids)
    return {"port": port, "killed": killed, "errors": errors}


# --------------------------------------------------------------- helpers


def _norm_dir(path: str) -> str:
    try:
        return str(Path(path).resolve()).lower()
    except (OSError, ValueError):
        return (path or "").lower()


def _registered_dir_names() -> Dict[str, str]:
    """Map every registered app's directory → display name.

    For bat-based apps the directory is the bat's parent; for
    claude-code apps it's the project dir. Used to label a running
    listener with the app it belongs to.
    """
    registry = load_registry()
    mapping: Dict[str, str] = {}
    for app_entry in registry.apps:
        if app_entry.project_dir:
            mapping[_norm_dir(app_entry.project_dir)] = app_entry.name
        if app_entry.bat_path:
            mapping[_norm_dir(str(Path(app_entry.bat_path).parent))] = app_entry.name
    return mapping


def _service_label(cmdline: List[str]) -> str:
    """Concise label for a (child) service from its command line.

    Generic across apps: the ``-m <module>`` target if present, else the
    first ``.py`` script's basename, else "". Used as the nested row's name
    so a helper reads as e.g. "src.tts_server" rather than repeating the
    parent app's name.
    """
    if not cmdline:
        return ""
    for i, tok in enumerate(cmdline):
        if tok == "-m" and i + 1 < len(cmdline):
            return cmdline[i + 1]
    for tok in cmdline[1:]:
        if tok.endswith(".py"):
            return Path(tok).name
    return ""


def _app_label_for_dir(cwd: str, dir_names: Dict[str, str]) -> str:
    """Best-effort app name for a process working directory.

    A registered app wins; otherwise the directory's own folder name
    (prettified) so an unregistered listener is still identifiable.
    """
    if not cwd:
        return ""
    name = dir_names.get(_norm_dir(cwd))
    if name:
        return name
    return pretty_folder_name(Path(cwd))
