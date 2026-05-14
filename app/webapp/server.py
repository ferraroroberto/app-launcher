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
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from src.app_config import load_app_config
from src.bat_generator import (
    discover_orphan_bats,
    discover_workspaces,
    run_generate,
)
from src.diagnostics import find_pids_on_port, kill_pids, list_app_listeners
from src.launcher import spawn_bat, spawn_claude
from src.registry import (
    AppEntry,
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
from src.sessions import discover_sessions, stop_session
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
        token = (self._get_token() or "").strip()
        if not token:
            return await call_next(request)

        client_host = request.client.host if request.client else ""
        if client_host in _LOOPBACK_HOSTS:
            return await call_next(request)

        path = request.url.path
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
        try:
            _launch_entry(entry, request.app.state.webapp_config)
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

    # ------------------------------------------------------ claude sessions
    @app.get("/api/claude-code/sessions")
    async def claude_sessions() -> Dict[str, Any]:
        sessions = discover_sessions(_claude_project_dirs(), with_titles=True)
        return {"sessions": [s.to_api() for s in sessions]}

    @app.post("/api/claude-code/sessions/{pid}/stop")
    async def stop_claude_session(pid: int) -> Dict[str, Any]:
        # Only stop PIDs that are actually discovered Claude Code
        # sessions — keeps this from being a generic process killer.
        sessions = discover_sessions(_claude_project_dirs())
        if not any(s.pid == pid for s in sessions):
            raise HTTPException(
                status_code=404,
                detail=f"no running Claude Code session with pid {pid}",
            )
        result = stop_session(pid)
        if not result.get("stopped"):
            raise HTTPException(
                status_code=500,
                detail=str(result.get("detail") or "failed to stop session"),
            )
        return result

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


def _claude_project_dirs() -> Dict[str, str]:
    """Map every registered claude-code project dir → its display name.

    Used to recognise which running ``claude`` processes are sessions
    the launcher knows about.
    """
    registry = load_registry()
    return {
        a.project_dir: a.name
        for a in registry.apps
        if a.kind == KIND_CLAUDE_CODE and a.project_dir
    }


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


def _launch_entry(entry: AppEntry, cfg: WebappConfig) -> None:
    """Dispatch a launch — claude for `claude-code` kind, bat for the rest."""
    if entry.kind == KIND_CLAUDE_CODE:
        if not entry.project_dir:
            raise OSError(f"claude-code entry {entry.id} has no project_dir")
        spawn_claude(Path(entry.project_dir), build_claude_flags(cfg))
        return
    if not entry.bat_path:
        raise OSError(f"app entry {entry.id} has no bat_path")
    spawn_bat(Path(entry.bat_path))


# Module-level app for `uvicorn app.webapp.server:app`.
app = create_app()
