"""Catch-all routes: index, healthz, install-ca, port probing + kill.

Port probe / kill live here because they're not about the app registry —
they're a generic "what's listening on this machine" diagnostic. The
listener→app label mapping uses the registry but doesn't mutate it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from src.diagnostics import find_pids_on_port, kill_pids, list_app_listeners
from src.registry import load_registry
from src.scanner import pretty_folder_name

from app.webapp.routers._helpers import STATIC_DIR

router = APIRouter()


@router.get("/")
async def index() -> FileResponse:
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=500, detail="index.html missing")
    return FileResponse(str(index_path))


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
    out = [
        {
            "port": owner.port,
            "pid": owner.pid,
            "name": owner.name,
            "exe": owner.exe,
            "cmdline": owner.cmdline_str(),
            "app": _app_label_for_dir(owner.cwd, dir_names),
        }
        for owner in list_app_listeners()
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
