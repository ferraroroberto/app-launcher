"""FastAPI webapp — phone-first launcher hub.

Routes (split across `app/webapp/routers/`):

    GET    /                                  → static/index.html         (misc)
    GET    /static/{file}                     → CSS / JS / icons          (static mount)
    GET    /healthz                           → liveness probe            (misc)
    GET    /install-ca                        → iOS .mobileconfig         (misc)

    POST   /api/login                         → swap password for token   (auth)
    GET    /api/config                        → host/port + scan + claude (config)
    POST   /api/config                        → patch + persist           (config)
    GET    /api/status                        → tunnel?, cert?, scan roots(config)

    GET    /api/apps                          → unified registry          (apps)
    POST   /api/apps/scan                     → walk scan_root            (apps)
    POST   /api/apps/save                     → persist selected          (apps)
    PATCH  /api/apps/{id}                     → rename                    (apps)
    DELETE /api/apps/{id}                     → remove                    (apps)
    POST   /api/apps/{id}/launch              → spawn bat or claude       (apps)

    GET    /api/ports/probe                   → psutil snapshot           (misc)
    POST   /api/ports/{port}/kill             → kill PID owning that port (misc)

    GET    /api/claude-code/flags             → persisted claude flags    (claude_code)
    GET    /api/claude-code/git-status        → per-project branch+dirty  (claude_code)
    GET    /api/claude-code/generate          → preview workspace↔bat     (claude_code)
    POST   /api/claude-code/generate          → workspace↔bat sync        (claude_code)
    GET    /api/claude-code/sessions          → running sessions          (sessions)
    POST   /api/claude-code/sessions/{sid}/stop                           (sessions)
    POST   /api/claude-code/sessions/{sid}/image                          (sessions)
    WS     /api/claude-code/sessions/{sid}/ws                             (sessions)

    /api/webauthn/*                           → passkey ceremonies        (webauthn)
"""

from __future__ import annotations

import logging
import mimetypes
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from src.app_config import load_app_config
from src.static_versioning import (
    compute_asset_hashes,
    fleet_hash_of,
    rewrite_js_imports,
)
from src.webapp_config import load_webapp_config
from src.webauthn_gate import WebAuthnGate

from app.webapp.middleware import BearerTokenMiddleware
from app.webapp.routers import (
    apps,
    auth,
    claude_code,
    config,
    jobs,
    life_os,
    misc,
    sessions,
    webauthn,
)
from app.webapp.routers._helpers import STATIC_DIR

_log = logging.getLogger(__name__)

_LONG_CACHE = "public, max-age=31536000, immutable"
_DAY_CACHE = "public, max-age=86400"
# Suffixes that get the year-long immutable cache. They go through the
# JS-import rewrite if .js; otherwise served as-is with the long header.
_HASHED_SUFFIXES = {".js", ".css"}
# Lightly cached (a day) — these change rarely but we don't want stale
# icons surviving for a year if we ever do swap them.
_DAY_CACHE_SUFFIXES = {".webmanifest", ".png", ".ico"}


class _VersionedStatic(StaticFiles):
    """Static mount that stamps Cache-Control + rewrites JS imports.

    JS files get their ``import './foo.js'`` calls rewritten to
    ``import './foo.js?v=<hash>'`` at serve time. Hashed assets get
    a year-long immutable cache; icons and manifest get a day; the
    iOS mobileconfig and anything else falls back to defaults.
    """

    def __init__(self, *, directory: str, asset_hashes: Dict[str, str]) -> None:
        super().__init__(directory=directory)
        self._asset_hashes = asset_hashes

    def file_response(
        self,
        full_path: os.PathLike,
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        path = Path(full_path)
        suffix = path.suffix.lower()

        if suffix == ".js":
            try:
                body = path.read_text(encoding="utf-8")
            except OSError:
                return super().file_response(full_path, stat_result, scope, status_code)
            rewritten = rewrite_js_imports(body, self._asset_hashes)
            media_type, _ = mimetypes.guess_type(str(path))
            return Response(
                content=rewritten,
                status_code=status_code,
                media_type=media_type or "text/javascript",
                headers={"Cache-Control": _LONG_CACHE},
            )

        response = super().file_response(full_path, stat_result, scope, status_code)
        if suffix in _HASHED_SUFFIXES:
            response.headers["Cache-Control"] = _LONG_CACHE
        elif suffix in _DAY_CACHE_SUFFIXES:
            response.headers["Cache-Control"] = _DAY_CACHE
        return response


@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield


def create_app() -> FastAPI:
    app_config = load_app_config()
    webapp_cfg = load_webapp_config()

    auth.ensure_log_handler()

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

    asset_hashes = compute_asset_hashes(STATIC_DIR)
    app.state.asset_hashes = asset_hashes
    app.state.asset_fleet_hash = fleet_hash_of(asset_hashes)
    if asset_hashes:
        _log.info(
            "ℹ️ Static assets stamped at fleet hash %s (%d files)",
            app.state.asset_fleet_hash,
            len(asset_hashes),
        )

    if STATIC_DIR.exists():
        app.mount(
            "/static",
            _VersionedStatic(directory=str(STATIC_DIR), asset_hashes=asset_hashes),
            name="static",
        )

    app.include_router(misc.router)
    app.include_router(auth.router)
    app.include_router(config.router)
    app.include_router(apps.router)
    app.include_router(jobs.router)
    app.include_router(sessions.router)
    app.include_router(claude_code.router)
    app.include_router(life_os.router)
    app.include_router(webauthn.router)

    return app


# Module-level app for `uvicorn app.webapp.server:app`.
app = create_app()
