"""Unified app registry — scan, add, rename, remove, launch.

`/api/apps` enriches tunnel-kind entries with a server-side /healthz
probe, behind a short TTL cache so the SPA's 4 s poll doesn't hammer
the sibling tunnels.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import requests
from fastapi import APIRouter, HTTPException, Request

from src import audit, session_client
from src.launcher import (
    open_local_terminal_window,
    spawn_bat,
    spawn_claude_session,
)
from src.registry import (
    decorate_for_api,
    discover_new,
    get_by_id,
    load_registry,
    persist_additions,
    remove_by_id,
    rename_by_id,
)
from src.scanner import KIND_CLAUDE_CODE, KIND_TUNNEL
from src.webapp_config import WebappConfig, build_claude_flags

from app.webapp.middleware import LOOPBACK_HOSTS
from app.webapp.routers._helpers import cert_present, client_ip, maybe_json

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/apps")
async def get_apps(request: Request) -> Dict[str, Any]:
    registry = load_registry()
    decorated = [decorate_for_api(a) for a in registry.apps]
    # Health for tunnel apps is probed here, server-side — the SPA
    # can't probe a sibling's /healthz from the browser (cross-origin,
    # no CORS headers, every probe would fail).
    tunnel_urls = [
        d["tunnel_url"]
        for d in decorated
        if d.get("kind") == KIND_TUNNEL and d.get("tunnel_url")
    ]
    health = await _health_for(tunnel_urls) if tunnel_urls else {}
    for d in decorated:
        if d.get("kind") == KIND_TUNNEL:
            d["health"] = health.get(d.get("tunnel_url"))
    return {
        "scan_root": registry.scan_root,
        "apps": decorated,
    }


@router.post("/api/apps/scan")
async def scan_apps(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    registry = load_registry()
    new = discover_new(
        projects_dir=Path(cfg.projects_dir),
        scan_root=Path(cfg.apps_scan_root),
        existing=registry,
    )
    return {"new": [decorate_for_api(a) for a in new]}


@router.post("/api/apps/save")
async def save_apps(request: Request) -> Dict[str, Any]:
    body = await maybe_json(request)
    cfg: WebappConfig = request.app.state.webapp_config
    selected_ids = set(body.get("ids") or [])
    if not selected_ids:
        raise HTTPException(status_code=400, detail="no ids provided")

    registry = load_registry()
    candidates = discover_new(
        projects_dir=Path(cfg.projects_dir),
        scan_root=Path(cfg.apps_scan_root),
        existing=registry,
    )
    keep = [c for c in candidates if c.id in selected_ids]
    added = persist_additions(registry, keep, Path(cfg.apps_scan_root))
    return {"added": [decorate_for_api(a) for a in added]}


@router.patch("/api/apps/{app_id}")
async def patch_app(app_id: str, request: Request) -> Dict[str, Any]:
    body = await maybe_json(request)
    new_name = str(body.get("name") or "").strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="name is required")
    registry = load_registry()
    entry = rename_by_id(registry, app_id, new_name)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"unknown app {app_id}")
    return {"app": decorate_for_api(entry)}


@router.delete("/api/apps/{app_id}")
async def delete_app(app_id: str) -> Dict[str, Any]:
    registry = load_registry()
    removed = remove_by_id(registry, app_id)
    if removed is None:
        raise HTTPException(status_code=404, detail=f"unknown app {app_id}")
    return {"removed": removed.id}


@router.post("/api/apps/{app_id}/launch")
async def launch_app(app_id: str, request: Request) -> Dict[str, Any]:
    registry = load_registry()
    entry = get_by_id(registry, app_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"unknown app {app_id}")
    cfg: WebappConfig = request.app.state.webapp_config

    # claude-code: two launch modes (chosen by the request body's
    # `mode`). "pty" (default) = a launcher-owned PTY session streamed
    # to and driven from the phone. "remote" = a detached console
    # window on the PC the session-host only tracks (listed + killable
    # but not streamed) — the Claude cloud app does the remote control.
    if entry.kind == KIND_CLAUDE_CODE:
        if not entry.project_dir:
            raise HTTPException(
                status_code=400,
                detail=f"claude-code entry {entry.id} has no project_dir",
            )
        body = await maybe_json(request)
        mode = str(body.get("mode") or "pty").strip().lower()

        if mode == "remote":
            try:
                session = await asyncio.to_thread(
                    spawn_claude_session,
                    Path(entry.project_dir),
                    entry.name,
                    build_claude_flags(cfg),
                    cfg.session_host_port,
                    "remote",
                )
            except session_client.SessionHostError as exc:
                raise HTTPException(status_code=exc.status, detail=str(exc))
            except OSError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            sid = str(session.get("session_id") or "")
            audit.audit_event(
                "remote_launch",
                session=sid,
                name=entry.name,
                project=entry.project_dir,
                client=client_ip(request),
            )
            return {
                "launched": entry.id,
                "name": entry.name,
                "kind": entry.kind,
                "mode": "remote",
                "session": session,
            }

        try:
            session = await asyncio.to_thread(
                spawn_claude_session,
                Path(entry.project_dir),
                entry.name,
                build_claude_flags(cfg),
                cfg.session_host_port,
            )
        except session_client.SessionHostError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc))
        except OSError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        sid = str(session.get("session_id") or "")
        audit.audit_event(
            "session_start",
            session=sid,
            name=entry.name,
            project=entry.project_dir,
            client=client_ip(request),
        )
        audit.session_log(
            sid, "start", name=entry.name, project=entry.project_dir
        )
        # Mirror the session into an interactive terminal window on the
        # PC. Skipped when the launch came from the PC itself (the
        # browser that launched it already shows the terminal). The PC
        # window connects over loopback, so it bypasses the Tailscale +
        # passkey gate — input works from both the phone and the PC.
        if (
            cfg.claude_show_local_window
            and client_ip(request) not in LOOPBACK_HOSTS
        ):
            scheme = "https" if cert_present() else "http"
            pc_url = f"{scheme}://127.0.0.1:{cfg.port}/?terminal={sid}"
            asyncio.create_task(
                asyncio.to_thread(open_local_terminal_window, pc_url)
            )
        return {
            "launched": entry.id,
            "name": entry.name,
            "kind": entry.kind,
            "mode": "pty",
            "session": session,
        }

    # everything else: a fresh visible CMD window running the bat.
    if not entry.bat_path:
        raise HTTPException(
            status_code=400, detail=f"app entry {entry.id} has no bat_path"
        )
    try:
        spawn_bat(Path(entry.bat_path))
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"launched": entry.id, "name": entry.name, "kind": entry.kind}


# --------------------------------------------------------------- helpers

# Health for tunnel apps — probed server-side, behind a short TTL cache
# so the 4 s /api/apps poll doesn't hammer the sibling tunnels.
_HEALTH_TTL_SECONDS = 5.0
_health_cache: Dict[str, Tuple[float, str]] = {}
_health_session = requests.Session()
_health_session.verify = False
try:  # silence the self-signed-cert warning on sibling probes
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:  # noqa: BLE001
    pass


def _probe_health_sync(tunnel_url: str) -> str:
    """Blocking GET ``<origin>/healthz`` → ``"up"`` | ``"down"``."""
    base = tunnel_url.split("?", 1)[0].rstrip("/")
    try:
        resp = _health_session.get(f"{base}/healthz", timeout=3)
        return "up" if resp.status_code == 200 else "down"
    except requests.RequestException:
        return "down"


async def _health_for(urls: List[str]) -> Dict[str, str]:
    """Health per url, served from a short TTL cache; cache misses are
    probed concurrently in worker threads so the event loop never blocks."""
    now = time.time()
    stale = [
        u
        for u in urls
        if now - _health_cache.get(u, (0.0, ""))[0] > _HEALTH_TTL_SECONDS
    ]
    if stale:
        results = await asyncio.gather(
            *(asyncio.to_thread(_probe_health_sync, u) for u in stale)
        )
        for url, status in zip(stale, results):
            _health_cache[url] = (now, status)
    return {u: _health_cache.get(u, (0.0, "down"))[1] for u in urls}
