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

from src import agents, app_runtime, audit, session_client
from src.app_config import AppConfig
from src.diagnostics import (
    detect_local_scheme,
    kill_process_tree,
    listening_port_for_pid_tree,
)
from src.launcher import (
    open_local_terminal_window,
    spawn_bat,
    spawn_claude_session,
)
from src.registry import (
    AppEntry,
    decorate_for_api,
    discover_new,
    get_by_id,
    live_claude_code_entries,
    load_registry,
    persist_additions,
    remove_by_id,
    rename_by_id,
)
from src.scanner import KIND_CLAUDE_CODE, KIND_TUNNEL
from src.webapp_config import (
    WebappConfig,
    build_antigravity_flags,
    build_claude_flags,
    build_codex_flags,
    build_copilot_flags,
    build_resume_flags,
)

from app.webapp.routers._helpers import (
    cert_present,
    client_ip,
    maybe_json,
    should_mirror_to_pc,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _claude_code_entries(cfg: WebappConfig) -> List[AppEntry]:
    """Live claude-code rows from the configured projects directory."""
    return live_claude_code_entries(
        Path(cfg.projects_dir), list(cfg.projects_ignore)
    )


@router.get("/api/apps")
async def get_apps(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    registry = load_registry()
    # claude-code rows are computed live from `projects_dir`; the
    # bat-based kinds come from the persisted registry. Any stale
    # claude-code row left in an older apps.json is ignored.
    bat_entries = [a for a in registry.apps if a.kind != KIND_CLAUDE_CODE]
    decorated = [
        decorate_for_api(a) for a in _claude_code_entries(cfg) + bat_entries
    ]
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
    # Mark which live coding rows the user has starred (issue #250). Bat-
    # based kinds never carry the flag — favorites are a Coding-tab concept.
    favorites = set(cfg.coding_favorites)
    for d in decorated:
        if d.get("kind") == KIND_CLAUDE_CODE:
            d["is_favorite"] = d.get("id") in favorites
    return {
        "scan_root": registry.scan_root,
        "apps": decorated,
    }


@router.post("/api/apps/scan")
async def scan_apps(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    registry = load_registry()
    # Walking the apps_scan_root tree is blocking I/O — keep it off the
    # event loop so the /api/apps 4 s poll and any concurrent request
    # stays responsive while a scan runs.
    new = await asyncio.to_thread(
        discover_new, scan_root=Path(cfg.apps_scan_root), existing=registry
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
        scan_root=Path(cfg.apps_scan_root), existing=registry
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
    cfg: WebappConfig = request.app.state.webapp_config
    registry = load_registry()
    # claude-code rows aren't persisted — resolve them against the live
    # directory scan first; bat-based rows come from the registry.
    entry = next(
        (e for e in _claude_code_entries(cfg) if e.id == app_id), None
    ) or get_by_id(registry, app_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"unknown app {app_id}")

    # claude-code (the Coding tab): two launch modes (chosen by the
    # request body's `mode`) and one of the registered coding agents
    # (`agent`). "pty" (default) = a launcher-owned PTY session streamed
    # to and driven from the phone. "remote" = a detached console window
    # on the PC the session-host only tracks (listed + killable but not
    # streamed). `agent` must be a key in `agents.AGENTS` (see
    # `src/agents.py` for the full set).
    if entry.kind == KIND_CLAUDE_CODE:
        if not entry.project_dir:
            raise HTTPException(
                status_code=400,
                detail=f"claude-code entry {entry.id} has no project_dir",
            )
        body = await maybe_json(request)
        mode = str(body.get("mode") or "pty").strip().lower()
        agent = str(body.get("agent") or agents.DEFAULT_AGENT).strip().lower()
        # Resume (issue #151): reopen the agent's own native session picker.
        # It swaps the normal flags for the agent's resume invocation but
        # honours the requested `mode` (issue #157): with Detached on, the
        # picker renders in the detached console window; with Detached off it
        # streams to the phone over a PTY. Resume no longer forces a PTY.
        resume = bool(body.get("resume"))
        # Phone's real terminal size (issue #126): a pty session spawns at
        # these dimensions so a ratatui TUI's first frame isn't cut. Absent
        # (older client, or remote mode) → the legacy 40×120 default.
        rows = int(body.get("rows") or 40)
        cols = int(body.get("cols") or 120)
        if agent not in agents.AGENTS:
            raise HTTPException(
                status_code=400, detail=f"unknown agent: {agent}"
            )
        # Claude Code is the launcher's core agent — its launch path is
        # unguarded, exactly as before issue #45. Other agents
        # (Antigravity, Copilot) are checked server-side too, as
        # defence-in-depth behind the Coding tab's already-disabled button.
        if agent != agents.DEFAULT_AGENT and not agents.is_installed(agent):
            raise HTTPException(
                status_code=400,
                detail=f"{agents.AGENTS[agent].label} is not installed",
            )
        # Each agent has its own flag set: Claude's model / effort /
        # always-on remote-control switches; Antigravity's two opt-in
        # launch toggles; Copilot's single allow-all toggle. The
        # non-Claude agents have no model/effort flags — that's chosen
        # in-TUI with `/model`.
        flag_builders = {
            "claude": build_claude_flags,
            "codex": build_codex_flags,
            "antigravity": build_antigravity_flags,
            "copilot": build_copilot_flags,
        }
        if resume:
            # Swap the normal flags for the agent's resume invocation; the
            # requested `mode` decides where its picker renders — a detached
            # console (mode="remote") or a streamed PTY (issue #157).
            flags = build_resume_flags(cfg, agent)
        else:
            flags = flag_builders[agent](cfg)

        if mode == "remote":
            try:
                session = await asyncio.to_thread(
                    spawn_claude_session,
                    Path(entry.project_dir),
                    entry.name,
                    flags,
                    cfg.session_host_port,
                    "remote",
                    agent,
                )
            except session_client.SessionHostError as exc:
                raise HTTPException(status_code=exc.status, detail=str(exc))
            except OSError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            sid = str(session.get("session_id") or "")
            audit.audit_event(
                "remote_launch",
                session=sid,
                agent=agent,
                name=entry.name,
                project=entry.project_dir,
                client=client_ip(request),
            )
            return {
                "launched": entry.id,
                "name": entry.name,
                "kind": entry.kind,
                "agent": agent,
                "mode": "remote",
                "session": session,
            }

        try:
            session = await asyncio.to_thread(
                spawn_claude_session,
                Path(entry.project_dir),
                entry.name,
                flags,
                cfg.session_host_port,
                "pty",
                agent,
                rows,
                cols,
            )
        except session_client.SessionHostError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc))
        except OSError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        sid = str(session.get("session_id") or "")
        audit.audit_event(
            "session_start",
            session=sid,
            agent=agent,
            name=entry.name,
            project=entry.project_dir,
            resume=resume,
            client=client_ip(request),
        )
        audit.session_log(
            sid, "start", agent=agent, name=entry.name, project=entry.project_dir
        )
        # Mirror the session into a dedicated interactive terminal window on
        # the PC for both phone and desktop-browser launches (issue #241 —
        # see should_mirror_to_pc); only a non-desktop loopback launch renders
        # in-page and skips it. The PC window connects over loopback,
        # bypassing the Tailscale + passkey gate.
        if should_mirror_to_pc(cfg.claude_show_local_window, request, body):
            scheme = "https" if cert_present() else "http"
            pc_url = f"{scheme}://127.0.0.1:{cfg.port}/?terminal={sid}"
            # Pass sid so launcher tracks the mirror window's HWND for
            # Stop & Close to dismiss it later (issue #20).
            asyncio.create_task(
                asyncio.to_thread(open_local_terminal_window, pc_url, sid)
            )
        return {
            "launched": entry.id,
            "name": entry.name,
            "kind": entry.kind,
            "agent": agent,
            "mode": "pty",
            "session": session,
        }

    # everything else: a fresh visible CMD window running the bat.
    if not entry.bat_path:
        raise HTTPException(
            status_code=400, detail=f"app entry {entry.id} has no bat_path"
        )
    try:
        pid = spawn_bat(Path(entry.bat_path))
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    app_runtime.record_spawn(entry.id, entry.name, entry.kind, pid)
    return {"launched": entry.id, "name": entry.name, "kind": entry.kind}


@router.get("/api/apps/running")
async def get_running_apps(request: Request) -> Dict[str, Any]:
    """List apps spawned from the launcher, each bound to its port + URL.

    State is in-process only (see :mod:`src.app_runtime`) — a webapp
    restart returns an empty list. ``port`` is null until a descendant
    of the spawned bat binds a socket; ``url`` is null when ``port`` is
    null or ``tailnet_host`` is unset.
    """
    app_runtime.prune_dead()
    app_config: AppConfig = request.app.state.app_config
    tailnet_host = (app_config.tailnet_host or "").strip()

    running: List[Dict[str, Any]] = []
    for inst in app_runtime.list_running():
        port = await asyncio.to_thread(listening_port_for_pid_tree, inst.pid)
        url = None
        if port and tailnet_host:
            # The app's scheme isn't known up front — probe it so the
            # phone doesn't get an https URL for a plain-HTTP Streamlit.
            scheme = await asyncio.to_thread(detect_local_scheme, port)
            url = f"{scheme}://{tailnet_host}:{port}/"
        running.append(
            {
                "app_id": inst.app_id,
                "name": inst.name,
                "kind": inst.kind,
                "pid": inst.pid,
                "started_at": int(inst.started_at),
                "port": port,
                "url": url,
                "alive": True,
            }
        )
    return {"running": running}


@router.post("/api/apps/{app_id}/instances/{pid}/stop")
async def stop_app_instance(app_id: str, pid: int) -> Dict[str, Any]:
    """Kill a launcher-spawned app instance's process tree.

    Returns 404 when ``(app_id, pid)`` isn't a tracked spawn — this
    endpoint must never kill an arbitrary PID it didn't launch.
    """
    if not app_runtime.is_tracked(app_id, pid):
        raise HTTPException(
            status_code=404,
            detail=f"no tracked instance {app_id}/{pid}",
        )
    await asyncio.to_thread(kill_process_tree, pid)
    app_runtime.forget_spawn(app_id, pid)
    return {"stopped": pid}


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
