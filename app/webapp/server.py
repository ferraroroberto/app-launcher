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
    GET    /api/claude-code/generate          → preview workspace↔bat     (claude_code)
    POST   /api/claude-code/generate          → workspace↔bat sync        (claude_code)
    GET    /api/claude-code/sessions          → running sessions          (sessions)
    POST   /api/claude-code/sessions/{sid}/stop                           (sessions)
    POST   /api/claude-code/sessions/{sid}/image                          (sessions)
    WS     /api/claude-code/sessions/{sid}/ws                             (sessions)

    /api/webauthn/*                           → passkey ceremonies        (webauthn)
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from src.app_config import load_app_config
from src.webapp_config import load_webapp_config
from src.webauthn_gate import WebAuthnGate

from app.webapp.middleware import BearerTokenMiddleware
from app.webapp.routers import (
    apps,
    auth,
    claude_code,
    config,
    misc,
    sessions,
    webauthn,
)
from app.webapp.routers._helpers import STATIC_DIR


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

    if STATIC_DIR.exists():
        app.mount(
            "/static", StaticFiles(directory=str(STATIC_DIR)), name="static"
        )

    app.include_router(misc.router)
    app.include_router(auth.router)
    app.include_router(config.router)
    app.include_router(apps.router)
    app.include_router(sessions.router)
    app.include_router(claude_code.router)
    app.include_router(webauthn.router)

    return app


# Module-level app for `uvicorn app.webapp.server:app`.
app = create_app()
