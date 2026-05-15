"""FastAPI webapp — phone-first launcher hub.

Routes:

    GET    /                                  → static/index.html
    GET    /static/{file}                     → CSS / JS / icons / manifest
    GET    /healthz                           → liveness probe
    GET    /install-ca                        → iOS .mobileconfig

    POST   /api/login                         → swap password for bearer token
    GET    /api/config                        → host/port + scan paths + claude flags
    POST   /api/config                        → patch + persist (whitelist)
    GET    /api/status                        → tunnel up?, cert?, scan roots

    GET    /api/apps                          → unified registry, grouped by kind
    POST   /api/apps/scan                     → walk scan_root, return diff
    POST   /api/apps/save                     → persist selected new entries
    PATCH  /api/apps/{id}                     → rename
    DELETE /api/apps/{id}                     → remove
    POST   /api/apps/{id}/launch              → spawn bat or claude in fresh CMD

    GET    /api/ports/probe                   → psutil snapshot of registered apps
    POST   /api/ports/{port}/kill             → kill PID owning that port

    GET    /api/claude-code/flags             → persisted claude flags
    POST   /api/claude-code/generate          → workspace ↔ remote.bat sync
    GET    /api/claude-code/sessions          → running claude sessions (process scan)
    POST   /api/claude-code/sessions/{pid}/stop → Ctrl+C, force tree-kill fallback
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect as ws_connect

from src import audit, session_client
from src.app_config import load_app_config
from src.bat_generator import (
    discover_orphan_bats,
    discover_workspaces,
    run_generate,
)
from src.diagnostics import find_pids_on_port, kill_pids, list_app_listeners
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
from src.scanner import (
    KIND_CLAUDE_CODE,
    KIND_TUNNEL,
    VALID_KINDS,
    pretty_folder_name,
)
from src.webauthn_gate import WebAuthnGate
from src.webapp_config import (
    ALWAYS_ON_CLAUDE_FLAGS,
    VALID_CLAUDE_EFFORTS,
    VALID_CLAUDE_MODELS,
    WebappConfig,
    build_claude_flags,
    load_webapp_config,
    update_webapp_config,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Loopback addresses bypass the bearer-token gate so local probes keep
# working without carrying the token. Tunnel traffic arrives with a
# non-loopback client IP and must present the token.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

_AUTH_EXEMPT_PREFIXES = ("/static/", "/healthz", "/install-ca")
_AUTH_EXEMPT_EXACT = frozenset({"/", "/healthz", "/install-ca", "/api/login"})

# Tailscale hands every node an address in the CGNAT range. The
# interactive terminal is gated to this range (plus loopback and an
# optional user allowlist) and is refused outright over the public tunnel.
_TAILNET_CGNAT = ipaddress.ip_network("100.64.0.0/10")
# Cloudflare's tunnel adds these headers — their presence means the
# request came in over the public edge, never acceptable for a terminal.
_CLOUDFLARE_HEADERS = ("cf-ray", "cf-connecting-ip")


def _via_cloudflare(headers) -> bool:
    return any(h in headers for h in _CLOUDFLARE_HEADERS)


def _client_in_tailnet(client_host: str, allowlist: List[str]) -> bool:
    """True when the client IP is loopback, in the tailnet, or allowlisted."""
    try:
        ip = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    if ip.is_loopback or ip in _TAILNET_CGNAT:
        return True
    for entry in allowlist or []:
        try:
            if ip in ipaddress.ip_network(str(entry), strict=False):
                return True
        except ValueError:
            if client_host == str(entry):
                return True
    return False


def _terminal_guard_level(path: str) -> Optional[str]:
    """Classify a request path's terminal-gating requirement.

    ``"passkey"`` — Tailscale-only **and** a valid passkey terminal token.
    ``"tailnet"`` — Tailscale-only (the WebAuthn ceremony endpoints).
    ``None``      — not a terminal endpoint; normal bearer-token rules apply.
    """
    if path.startswith("/api/webauthn/"):
        return "tailnet"
    if path.startswith("/api/claude-code/sessions/") and path.endswith("/image"):
        return "passkey"
    return None


def _terminal_http_gate(request: Request) -> Optional[JSONResponse]:
    """Enforce Tailscale-only (+ passkey) access on terminal HTTP endpoints.

    Returns an error response to short-circuit with, or ``None`` to allow.
    Loopback callers are handled by the middleware before this runs.
    """
    level = _terminal_guard_level(request.url.path)
    if level is None:
        return None
    if _via_cloudflare(request.headers):
        return JSONResponse(
            status_code=403,
            content={"detail": "terminal endpoints are not reachable over the public tunnel"},
        )
    cfg = request.app.state.webapp_config
    client_host = request.client.host if request.client else ""
    if not _client_in_tailnet(client_host, getattr(cfg, "tailnet_allowlist", [])):
        return JSONResponse(
            status_code=403,
            content={"detail": "terminal endpoints are Tailscale-only"},
        )
    if level == "passkey" and WebAuthnGate.configured(cfg):
        gate: WebAuthnGate = request.app.state.webauthn_gate
        presented = request.headers.get("x-terminal-token") or (
            request.query_params.get("tt", "")
        )
        if not gate.valid_terminal_token(presented):
            return JSONResponse(
                status_code=401,
                content={"detail": "passkey unlock required"},
            )
    return None


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _terminal_reachability(request: Request) -> Dict[str, Any]:
    """Can the *current* connection reach the live terminal at all?

    The terminal is Tailscale-only by design — so the SPA can ask up front
    and explain it, rather than letting the user open a terminal that will
    only ever say "Disconnected". Used by ``/api/status``.
    """
    client_host = request.client.host if request.client else ""
    if client_host in _LOOPBACK_HOSTS:
        return {"reachable": True, "reason": "loopback"}
    if _via_cloudflare(request.headers):
        return {
            "reachable": False,
            "reason": (
                "The live terminal is Tailscale-only — it is blocked on the "
                "public Cloudflare tunnel by design. Open the launcher over "
                "your Tailscale URL (https://<pc>.<tailnet>.ts.net:8445) to "
                "use it."
            ),
        }
    cfg = request.app.state.webapp_config
    if not _client_in_tailnet(client_host, getattr(cfg, "tailnet_allowlist", [])):
        return {
            "reachable": False,
            "reason": (
                f"This connection ({client_host}) is not on your tailnet. "
                "Open the launcher over your Tailscale URL, or add this "
                "network to tailnet_allowlist in config/webapp_config.json."
            ),
        }
    return {"reachable": True, "reason": "tailnet"}


auth_logger = logging.getLogger("launcher.auth")
_AUTH_LOG_PATH = PROJECT_ROOT / "webapp" / "auth.log"


def _ensure_auth_log_handler() -> None:
    if any(
        isinstance(h, logging.FileHandler)
        and Path(h.baseFilename).resolve() == _AUTH_LOG_PATH.resolve()
        for h in auth_logger.handlers
    ):
        return
    try:
        _AUTH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(_AUTH_LOG_PATH, encoding="utf-8")
        fh.setLevel(logging.INFO)
        fh.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        auth_logger.addHandler(fh)
        auth_logger.setLevel(logging.INFO)
    except OSError as exc:
        logger.warning(f"⚠️  Could not open {_AUTH_LOG_PATH}: {exc}")


_ensure_auth_log_handler()


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Require Authorization: Bearer <token> on API endpoints (non-loopback only)."""

    def __init__(self, app, get_token):
        super().__init__(app)
        self._get_token = get_token

    async def dispatch(self, request: Request, call_next):
        client_host = request.client.host if request.client else ""
        is_loopback = client_host in _LOOPBACK_HOSTS
        path = request.url.path

        # Terminal endpoints are Tailscale-only (+ passkey for the
        # interactive ones). Enforced even when no bearer token is
        # configured. The PC itself (loopback) is trusted and skips it.
        if not is_loopback:
            gate_err = _terminal_http_gate(request)
            if gate_err is not None:
                return gate_err

        token = (self._get_token() or "").strip()
        if not token or is_loopback:
            return await call_next(request)

        if path in _AUTH_EXEMPT_EXACT or any(
            path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES
        ):
            return await call_next(request)

        presented = ""
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            presented = auth_header[7:].strip()
        if not presented:
            presented = request.query_params.get("token", "").strip()

        if presented and hmac.compare_digest(presented, token):
            return await call_next(request)

        return JSONResponse(
            status_code=401,
            content={"detail": "missing or invalid bearer token"},
            headers={"WWW-Authenticate": 'Bearer realm="launcher"'},
        )


@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield


def create_app() -> FastAPI:
    app_config = load_app_config()
    webapp_cfg = load_webapp_config()

    app = FastAPI(
        title="Launcher",
        version="0.1.0",
        lifespan=_lifespan,
    )

    app.add_middleware(
        BearerTokenMiddleware,
        get_token=lambda: getattr(app.state.webapp_config, "auth_token", ""),
    )

    app.state.app_config = app_config
    app.state.webapp_config = webapp_cfg
    app.state.webauthn_gate = WebAuthnGate()

    # ------------------------------------------------------ static
    if STATIC_DIR.exists():
        app.mount(
            "/static", StaticFiles(directory=str(STATIC_DIR)), name="static"
        )

    @app.get("/")
    async def index() -> FileResponse:
        index_path = STATIC_DIR / "index.html"
        if not index_path.exists():
            raise HTTPException(status_code=500, detail="index.html missing")
        return FileResponse(str(index_path))

    @app.get("/healthz")
    async def healthz() -> Dict[str, Any]:
        return {"ok": True, "service": "launcher"}

    @app.get("/install-ca")
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

    # ------------------------------------------------------ config
    @app.get("/api/config")
    async def get_config(request: Request) -> Dict[str, Any]:
        cfg: WebappConfig = request.app.state.webapp_config
        return {
            "host": cfg.host,
            "port": cfg.port,
            "projects_dir": cfg.projects_dir,
            "apps_scan_root": cfg.apps_scan_root,
            "claude": {
                "model": cfg.claude_model,
                "effort": cfg.claude_effort,
                "verbose": cfg.claude_verbose,
                "debug": cfg.claude_debug,
                "models_available": list(VALID_CLAUDE_MODELS),
                "efforts_available": list(VALID_CLAUDE_EFFORTS),
                "always_on_flags": list(ALWAYS_ON_CLAUDE_FLAGS),
                "computed_flags": build_claude_flags(cfg),
            },
            "auth_password_set": bool(cfg.auth_password),
        }

    @app.post("/api/config")
    async def patch_config(request: Request) -> Dict[str, Any]:
        body = await request.json()
        allowed = {
            "projects_dir",
            "apps_scan_root",
            "claude_model",
            "claude_effort",
            "claude_verbose",
            "claude_debug",
        }
        patch = {k: v for k, v in body.items() if k in allowed}
        try:
            new_cfg = update_webapp_config(**patch)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        request.app.state.webapp_config = new_cfg
        return {
            "ok": True,
            "claude_flags": build_claude_flags(new_cfg),
        }

    @app.post("/api/login")
    async def login(request: Request) -> Dict[str, Any]:
        cfg: WebappConfig = request.app.state.webapp_config
        client_host = request.client.host if request.client else "?"
        if not cfg.auth_password:
            auth_logger.info(
                f"⚠️  Login attempt from {client_host} but no auth_password configured"
            )
            raise HTTPException(
                status_code=503, detail="password auth not configured"
            )
        if not cfg.auth_token:
            auth_logger.info(
                f"⚠️  Login attempt from {client_host} but no auth_token configured"
            )
            raise HTTPException(
                status_code=503, detail="bearer token not configured"
            )
        body = await _maybe_json(request)
        presented = str(body.get("password") or "")
        if not presented or not hmac.compare_digest(presented, cfg.auth_password):
            auth_logger.warning(
                f"🚨 Failed password attempt from {client_host} "
                f"(presented: {len(presented)} chars)"
            )
            raise HTTPException(status_code=401, detail="bad password")
        auth_logger.info(f"🔓 Password login from {client_host}")
        return {"token": cfg.auth_token}

    @app.get("/api/status")
    async def status(request: Request) -> Dict[str, Any]:
        cfg: WebappConfig = request.app.state.webapp_config
        tunnel_file = PROJECT_ROOT / "webapp" / "last_tunnel_url.txt"
        tunnel_url: Optional[str] = None
        if tunnel_file.exists():
            try:
                tunnel_url = tunnel_file.read_text(encoding="utf-8").strip() or None
            except OSError:
                tunnel_url = None
        return {
            "projects_dir": cfg.projects_dir,
            "apps_scan_root": cfg.apps_scan_root,
            "tunnel_url": tunnel_url,
            "tls": _cert_present(),
            "terminal": _terminal_reachability(request),
        }

    # ------------------------------------------------------ apps registry
    @app.get("/api/apps")
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

    @app.post("/api/apps/scan")
    async def scan_apps(request: Request) -> Dict[str, Any]:
        cfg: WebappConfig = request.app.state.webapp_config
        registry = load_registry()
        new = discover_new(
            projects_dir=Path(cfg.projects_dir),
            scan_root=Path(cfg.apps_scan_root),
            existing=registry,
        )
        return {"new": [decorate_for_api(a) for a in new]}

    @app.post("/api/apps/save")
    async def save_apps(request: Request) -> Dict[str, Any]:
        body = await _maybe_json(request)
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

    @app.patch("/api/apps/{app_id}")
    async def patch_app(app_id: str, request: Request) -> Dict[str, Any]:
        body = await _maybe_json(request)
        new_name = str(body.get("name") or "").strip()
        if not new_name:
            raise HTTPException(status_code=400, detail="name is required")
        registry = load_registry()
        entry = rename_by_id(registry, app_id, new_name)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"unknown app {app_id}")
        return {"app": decorate_for_api(entry)}

    @app.delete("/api/apps/{app_id}")
    async def delete_app(app_id: str) -> Dict[str, Any]:
        registry = load_registry()
        removed = remove_by_id(registry, app_id)
        if removed is None:
            raise HTTPException(status_code=404, detail=f"unknown app {app_id}")
        return {"removed": removed.id}

    @app.post("/api/apps/{app_id}/launch")
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
            body = await _maybe_json(request)
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
                    client=_client_ip(request),
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
                client=_client_ip(request),
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
                and _client_ip(request) not in _LOOPBACK_HOSTS
            ):
                scheme = "https" if _cert_present() else "http"
                pc_url = f"{scheme}://127.0.0.1:{cfg.port}/?terminal={sid}"
                asyncio.create_task(
                    _open_and_attach_mirror(
                        pc_url, sid, cfg.session_host_port
                    )
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

    # ------------------------------------------------------ ports / kill
    @app.get("/api/ports/probe")
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

    @app.post("/api/ports/{port}/kill")
    async def kill_port(port: int) -> Dict[str, Any]:
        if port < 1 or port > 65535:
            raise HTTPException(status_code=400, detail="port out of range")
        pids = find_pids_on_port(port)
        if not pids:
            return {"port": port, "killed": [], "detail": "nothing was listening"}
        killed, errors = kill_pids(pids)
        return {"port": port, "killed": killed, "errors": errors}

    # ------------------------------------------------------ claude code
    @app.get("/api/claude-code/flags")
    async def claude_flags(request: Request) -> Dict[str, Any]:
        cfg: WebappConfig = request.app.state.webapp_config
        return {
            "model": cfg.claude_model,
            "effort": cfg.claude_effort,
            "verbose": cfg.claude_verbose,
            "debug": cfg.claude_debug,
            "models_available": list(VALID_CLAUDE_MODELS),
            "efforts_available": list(VALID_CLAUDE_EFFORTS),
            "always_on_flags": list(ALWAYS_ON_CLAUDE_FLAGS),
            "computed_flags": build_claude_flags(cfg),
        }

    @app.get("/api/claude-code/generate")
    async def claude_generate_preview(request: Request) -> Dict[str, Any]:
        """Preview what `POST /api/claude-code/generate` would do."""
        cfg: WebappConfig = request.app.state.webapp_config
        projects_dir = Path(cfg.projects_dir)
        return {
            "projects_dir": str(projects_dir),
            "workspaces": [
                {
                    "name": w.name,
                    "project_dir": str(w.project_dir),
                    "bat_name": w.bat_name,
                    "bat_exists": w.bat_exists,
                }
                for w in discover_workspaces(projects_dir)
            ],
            "orphans": [
                {
                    "name": o.name,
                    "project_dir": str(o.project_dir),
                    "bat_name": o.bat_name,
                    "ws_name": o.ws_name,
                }
                for o in discover_orphan_bats(projects_dir)
            ],
        }

    @app.post("/api/claude-code/generate")
    async def claude_generate(request: Request) -> Dict[str, Any]:
        body = await _maybe_json(request)
        cfg: WebappConfig = request.app.state.webapp_config
        overwrite = set(body.get("overwrite") or [])
        create_ws = set(body.get("create_ws") or [])
        result = run_generate(
            projects_dir=Path(cfg.projects_dir),
            flags=build_claude_flags(cfg),
            overwrite_names=overwrite,
            create_ws_names=create_ws,
        )
        return {
            "created": result.created,
            "overwritten": result.overwritten,
            "ws_created": result.ws_created,
            "errors": result.errors,
        }

    # ------------------------------------------------ claude PTY sessions
    @app.get("/api/claude-code/sessions")
    async def claude_sessions(request: Request) -> Dict[str, Any]:
        """List launcher-owned PTY sessions (public, token-gated)."""
        cfg: WebappConfig = request.app.state.webapp_config
        try:
            sessions = await asyncio.to_thread(
                session_client.list_sessions, cfg.session_host_port
            )
        except session_client.SessionHostError as exc:
            logger.debug(f"session list failed: {exc}")
            sessions = []
        return {"sessions": sessions}

    @app.post("/api/claude-code/sessions/{sid}/stop")
    async def stop_claude_session(sid: str, request: Request) -> Dict[str, Any]:
        """Stop a PTY session — quit | interrupt | kill (public, token-gated).

        ``close_window`` (bool, default false) also dismisses the PC-side
        window: for PTY sessions that's the Edge/Chrome ``--app`` mirror
        (killed by stashed PID); for detached sessions it's a tree taskkill
        of the cmd console. Without it, detached sessions kill only the
        inner ``claude.exe`` so the cmd shell — and the window — stay open.
        """
        cfg: WebappConfig = request.app.state.webapp_config
        body = await _maybe_json(request)
        mode = str(body.get("mode") or "quit")
        close_window = bool(body.get("close_window") or False)
        try:
            result = await asyncio.to_thread(
                session_client.stop,
                cfg.session_host_port,
                sid,
                mode,
                close_window,
            )
        except session_client.SessionHostError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc))
        audit.audit_event(
            "session_stop",
            session=sid,
            mode=mode,
            close_window=close_window,
            client=_client_ip(request),
        )
        audit.session_log(sid, "stop", mode=mode, close_window=close_window)
        return result

    @app.post("/api/claude-code/sessions/{sid}/image")
    async def session_image(
        sid: str, request: Request, file: UploadFile = File(...)
    ) -> Dict[str, Any]:
        """Upload an image into a session (Tailscale-only + passkey)."""
        cfg: WebappConfig = request.app.state.webapp_config
        content = await file.read()
        try:
            result = await asyncio.to_thread(
                session_client.upload_image,
                cfg.session_host_port,
                sid,
                file.filename or "image.png",
                content,
                file.content_type or "application/octet-stream",
            )
        except session_client.SessionHostError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc))
        audit.session_log(
            sid, "image", path=result.get("path"), bytes=len(content)
        )
        return result

    @app.websocket("/api/claude-code/sessions/{sid}/ws")
    async def proxy_session_ws(websocket: WebSocket, sid: str) -> None:
        """Tailscale-only + passkey-gated WebSocket proxy to the session-host.

        Browser ⇄ webapp ⇄ session-host. The webapp is the single auth
        choke point — WebSockets bypass the HTTP middleware, so the same
        Tailscale + bearer + passkey checks are re-applied here.
        """
        cfg: WebappConfig = websocket.app.state.webapp_config
        gate: WebAuthnGate = websocket.app.state.webauthn_gate
        client_host = websocket.client.host if websocket.client else ""

        # Accept first, *then* gate — so the browser receives the close
        # code + reason and can show a clear message instead of a bare
        # "Disconnected" (closing before accept just fails the handshake).
        await websocket.accept()

        if client_host not in _LOOPBACK_HOSTS:
            if _via_cloudflare(websocket.headers):
                await websocket.close(
                    code=4403,
                    reason="terminal is Tailscale-only — blocked on the public tunnel",
                )
                return
            if not _client_in_tailnet(
                client_host, getattr(cfg, "tailnet_allowlist", [])
            ):
                await websocket.close(
                    code=4403, reason="terminal is Tailscale-only"
                )
                return
            token = (cfg.auth_token or "").strip()
            if token:
                presented = websocket.query_params.get("token", "").strip()
                if not (presented and hmac.compare_digest(presented, token)):
                    await websocket.close(
                        code=4401, reason="missing or invalid bearer token"
                    )
                    return
            if WebAuthnGate.configured(cfg):
                tt = websocket.query_params.get("tt", "").strip()
                if not gate.valid_terminal_token(tt):
                    await websocket.close(
                        code=4401, reason="passkey unlock required"
                    )
                    return

        # The phone drives the PTY size; the loopback PC mirror window
        # connects as role=pc and never resizes it (see session-host).
        role = "pc" if client_host in _LOOPBACK_HOSTS else "phone"
        upstream_url = session_client.ws_url(cfg.session_host_port, sid, role)
        try:
            async with ws_connect(upstream_url) as upstream:
                audit.audit_event(
                    "ws_open", session=sid, client=_client_ip_ws(websocket)
                )
                audit.session_log(sid, "ws_open", client=_client_ip_ws(websocket))
                await _proxy_websocket(websocket, upstream, sid)
        except (OSError, WebSocketDisconnect) as exc:
            logger.debug(f"WS proxy {sid[:8]} ended: {exc}")
            try:
                await websocket.close(
                    code=4502, reason="session-host unreachable"
                )
            except RuntimeError:
                pass
        finally:
            audit.session_log(sid, "ws_close")

    # ------------------------------------------------------ webauthn gate
    @app.get("/api/webauthn/status")
    async def webauthn_status(request: Request) -> Dict[str, Any]:
        cfg: WebappConfig = request.app.state.webapp_config
        gate: WebAuthnGate = request.app.state.webauthn_gate
        return {
            "configured": WebAuthnGate.configured(cfg),
            "rp_id": cfg.webauthn_rp_id,
            "enrollment_open": gate.enrollment_open(),
            "enrollment_seconds_left": gate.enrollment_seconds_left(),
            "devices": gate.list_devices(),
        }

    @app.post("/api/webauthn/enroll/window")
    async def webauthn_open_window(request: Request) -> Dict[str, Any]:
        """Open the one-time passkey enrollment window. PC-only (loopback).

        Called by the tray menu item — opening it deliberately from the PC
        is what makes adding a new device a conscious act.
        """
        client_host = request.client.host if request.client else ""
        if client_host not in _LOOPBACK_HOSTS:
            raise HTTPException(
                status_code=403,
                detail="the enrollment window can only be opened from the PC",
            )
        gate: WebAuthnGate = request.app.state.webauthn_gate
        body = await _maybe_json(request)
        seconds = min(max(float(body.get("seconds") or 300), 30.0), 900.0)
        gate.open_enrollment_window(seconds)
        audit.audit_event("enroll_window_opened", seconds=int(seconds))
        return {
            "enrollment_open": True,
            "seconds": gate.enrollment_seconds_left(),
        }

    @app.post("/api/webauthn/enroll/begin")
    async def webauthn_enroll_begin(request: Request) -> Dict[str, Any]:
        cfg: WebappConfig = request.app.state.webapp_config
        gate: WebAuthnGate = request.app.state.webauthn_gate
        if not WebAuthnGate.configured(cfg):
            raise HTTPException(status_code=503, detail="webauthn not configured")
        body = await _maybe_json(request)
        label = str(body.get("label") or "device").strip()[:60] or "device"
        try:
            options = gate.begin_registration(cfg, label)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        audit.audit_event(
            "enroll_begin", label=label, client=_client_ip(request)
        )
        return options

    @app.post("/api/webauthn/enroll/finish")
    async def webauthn_enroll_finish(request: Request) -> Dict[str, Any]:
        cfg: WebappConfig = request.app.state.webapp_config
        gate: WebAuthnGate = request.app.state.webauthn_gate
        credential = await _maybe_json(request)
        try:
            result = gate.finish_registration(cfg, credential)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        except Exception as exc:  # noqa: BLE001 — verification failure
            audit.audit_event(
                "enroll_fail", error=str(exc), client=_client_ip(request)
            )
            raise HTTPException(
                status_code=400, detail=f"registration failed: {exc}"
            )
        audit.audit_event(
            "enroll_ok",
            device=result.get("id"),
            label=result.get("label"),
            client=_client_ip(request),
        )
        return result

    @app.post("/api/webauthn/auth/begin")
    async def webauthn_auth_begin(request: Request) -> Dict[str, Any]:
        cfg: WebappConfig = request.app.state.webapp_config
        gate: WebAuthnGate = request.app.state.webauthn_gate
        if not WebAuthnGate.configured(cfg):
            raise HTTPException(status_code=503, detail="webauthn not configured")
        try:
            return gate.begin_authentication(cfg)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc))

    @app.post("/api/webauthn/auth/finish")
    async def webauthn_auth_finish(request: Request) -> Dict[str, Any]:
        cfg: WebappConfig = request.app.state.webapp_config
        gate: WebAuthnGate = request.app.state.webauthn_gate
        credential = await _maybe_json(request)
        try:
            token = gate.finish_authentication(cfg, credential)
        except PermissionError as exc:
            audit.audit_event(
                "auth_fail", error=str(exc), client=_client_ip(request)
            )
            raise HTTPException(status_code=403, detail=str(exc))
        except Exception as exc:  # noqa: BLE001 — verification failure
            audit.audit_event(
                "auth_fail", error=str(exc), client=_client_ip(request)
            )
            raise HTTPException(
                status_code=400, detail=f"authentication failed: {exc}"
            )
        audit.audit_event("auth_ok", client=_client_ip(request))
        return {"terminal_token": token, "ttl_seconds": 12 * 3600}

    @app.delete("/api/webauthn/devices/{device_id}")
    async def webauthn_remove_device(
        device_id: str, request: Request
    ) -> Dict[str, Any]:
        gate: WebAuthnGate = request.app.state.webauthn_gate
        if not gate.remove_device(device_id):
            raise HTTPException(status_code=404, detail="unknown device")
        audit.audit_event(
            "device_removed", device=device_id, client=_client_ip(request)
        )
        return {"removed": device_id}

    return app


# --------------------------------------------------------------- helpers


async def _maybe_json(request: Request) -> Dict[str, Any]:
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _cert_present() -> bool:
    return (
        (PROJECT_ROOT / "webapp" / "certificates" / "cert.pem").exists()
        and (PROJECT_ROOT / "webapp" / "certificates" / "key.pem").exists()
    )


async def _open_and_attach_mirror(
    pc_url: str, sid: str, session_host_port: int
) -> None:
    """Open the PC-side mirror window and stash its PID on the session.

    Backs the "Stop & Close" button — without the PID the session-host has
    no handle to dismiss the window. Best-effort: a missed PID just means
    "Stop & Close" degrades to "Stop" (window stays open) for that session.
    """
    pid = await asyncio.to_thread(open_local_terminal_window, pc_url)
    if not pid:
        return
    try:
        await asyncio.to_thread(
            session_client.attach_mirror, session_host_port, sid, pid
        )
    except session_client.SessionHostError as exc:
        logger.debug(f"attach_mirror({sid[:8]}, pid={pid}) failed: {exc}")


def _client_ip_ws(websocket: WebSocket) -> str:
    return websocket.client.host if websocket.client else "?"


async def _proxy_websocket(client: WebSocket, upstream, sid: str) -> None:
    """Pump frames both ways between the browser and the session-host.

    Server→client frames are raw terminal output. Client→server frames are
    JSON control messages — ``input`` frames are tee'd to the per-session
    audit log on the way through.
    """

    async def client_to_upstream() -> None:
        while True:
            raw = await client.receive_text()
            try:
                msg = json.loads(raw)
                if isinstance(msg, dict) and msg.get("type") == "input":
                    audit.session_input(sid, str(msg.get("data") or ""))
            except (ValueError, TypeError):
                pass
            await upstream.send(raw)

    async def upstream_to_client() -> None:
        async for message in upstream:
            if isinstance(message, bytes):
                message = message.decode("utf-8", errors="replace")
            await client.send_text(message)
        # session-host closed its side (session ended) — close the browser.
        await client.close(code=4000)

    c2u = asyncio.create_task(client_to_upstream())
    u2c = asyncio.create_task(upstream_to_client())
    done, pending = await asyncio.wait(
        {c2u, u2c}, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
    for task in done:
        exc = task.exception()
        if exc and not isinstance(exc, WebSocketDisconnect):
            logger.debug(f"WS proxy {sid[:8]} task ended: {exc}")


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


# Module-level app for `uvicorn app.webapp.server:app`.
app = create_app()
