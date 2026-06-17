"""Catch-all routes: index, healthz, install-ca, port probing + kill.

Port probe / kill live here because they're not about the app registry —
they're a generic "what's listening on this machine" diagnostic. The
listener→app label mapping uses the registry but doesn't mutate it.
"""

from __future__ import annotations

import datetime as _dt
import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from src.agents import detect_agents
from src.diagnostics import find_pids_on_port, kill_pids, list_app_listeners
from src.registry import load_registry
from src.scanner import pretty_folder_name
from src.static_versioning import asset_hash_for, rewrite_index_html

from app.webapp.routers._helpers import PROJECT_ROOT, STATIC_DIR

_log = logging.getLogger(__name__)

router = APIRouter()


def _resolve_git_sha() -> str:
    """Short git SHA, captured once at module import.

    Falls back to ``"unknown"`` if git isn't on PATH or this isn't a
    repo — both happen in test envs and shouldn't crash startup. The
    pythonw tray has no console, so we pass ``CREATE_NO_WINDOW`` to
    keep a stray cmd from flashing AND to avoid the subprocess
    failing on console-allocation quirks when its parent has none.
    """
    # ``-C <path>`` is more robust than ``cwd=`` when something has
    # already chdir'd; both belt and braces here.
    cmd = ["git", "-C", str(PROJECT_ROOT), "rev-parse", "--short", "HEAD"]
    kwargs: Dict[str, Any] = dict(
        capture_output=True,
        # pythonw has no stdin handle — without this, subprocess on
        # Windows can fail with WinError 6 (invalid handle) before
        # git even runs.
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=5,
        check=False,
    )
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(cmd, **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning("⚠️ /api/version: git rev-parse raised %s: %s", type(exc).__name__, exc)
        return "unknown"
    sha = (result.stdout or "").strip()
    if not sha:
        _log.warning(
            "⚠️ /api/version: git rev-parse exit=%s stderr=%r",
            result.returncode,
            (result.stderr or "").strip(),
        )
        return "unknown"
    return sha


_GIT_SHA = _resolve_git_sha()
_BUILT_AT = _dt.datetime.now().replace(microsecond=0).isoformat()


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
    """THROWAWAY de-risking spike (#246): a hands-free voice-loop prototype.

    Served through the same ``rewrite_index_html`` + no-cache path as ``/`` so
    its module script picks up the asset hash (and never serves stale across
    builds). Bearer-gated like every page (``?token=`` accepted); the page
    bootstraps the passkey terminal token itself. Delete this route with the
    spike-voice-loop.* files once the viability gate is answered.
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
async def version(request: Request) -> Dict[str, str]:
    """Build identity. Stable across requests; cached at module load."""
    asset_hashes = getattr(request.app.state, "asset_hashes", {}) or {}
    return {
        "git_sha": _GIT_SHA,
        "built_at": _BUILT_AT,
        "asset_hash": asset_hash_for(asset_hashes, "styles.css") or "",
    }


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


@router.get("/install-ca")
async def install_ca() -> FileResponse:
    profile = STATIC_DIR / "launcher-ca.mobileconfig"
    if not profile.exists():
        raise HTTPException(
            status_code=404,
            detail=(
                "CA profile not generated yet. Run "
                "`scripts/gen_ssl_cert.py` from the project root."
            ),
        )
    return FileResponse(
        str(profile),
        media_type="application/x-apple-aspen-config",
        filename="launcher-ca.mobileconfig",
    )


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
