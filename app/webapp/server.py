"""FastAPI webapp — phone-first launcher hub.

Routes are split across `app/webapp/routers/`; see each module for the
full per-family surface.  Top-level families:

    misc         GET  /                         → static/index.html
                 GET  /static/{file}            → CSS / JS / icons (static mount)
                 GET  /healthz                  → liveness probe
                 GET  /api/version              → git_sha + asset_hash
                 GET  /api/agents               → registered coding agents

    auth         POST /api/login                → swap password for token

    config       GET  /api/config               → host/port + scan + agent flags
                 POST /api/config               → patch + persist
                 GET  /api/status               → tunnel?, cert?, scan roots
                 GET  /api/ports/probe          → psutil snapshot
                 POST /api/ports/{port}/kill    → kill PID owning that port

    apps         GET  /api/apps                 → unified registry
                 POST /api/apps/scan            → walk scan_root
                 POST /api/apps/save            → persist selected
                 PATCH  /api/apps/{id}          → rename
                 DELETE /api/apps/{id}          → remove
                 POST /api/apps/{id}/launch     → spawn bat or coding-agent session

    sessions     GET  /api/claude-code/sessions           → running sessions
                 POST /api/claude-code/sessions/{sid}/stop
                 POST /api/claude-code/sessions/{sid}/image
                 WS   /api/claude-code/sessions/{sid}/ws
                 POST /api/transcribe                     → one-shot audio → text
                 POST /api/transcribe/sessions            → start streaming dictation
                 POST /api/transcribe/sessions/{id}/chunk → append audio chunk
                 POST /api/transcribe/sessions/{id}/finish
                 GET  /api/ocr                            → screenshot OCR

    claude_code  GET  /api/claude-code/flags              → persisted per-agent flags
                 GET  /api/claude-code/git-status         → per-project branch+dirty
                 GET  /api/claude-code/generate           → preview workspace↔bat
                 POST /api/claude-code/generate           → workspace↔bat sync

    jobs         /api/jobs/*                   → Jobs tab (~30 routes)

    life_os      /api/life-os/*                → Life OS tab

    webauthn     /api/webauthn/*               → passkey ceremonies
"""

from __future__ import annotations

import asyncio
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

from src import launcher, session_client
from src.app_config import load_app_config
from src.static_versioning import (
    compute_asset_hashes,
    fleet_hash_of,
    rewrite_js_imports,
)
from src.webapp_config import SESSION_HOST_PORT_ENV, load_webapp_config
from src.webauthn_gate import WebAuthnGate

from app.webapp.middleware import BearerTokenMiddleware
from app.webapp.observability import (
    SlowRequestLogMiddleware,
    ensure_slow_log_handler,
)
from app.webapp.routers import (
    apps,
    auth,
    board,
    claude_code,
    config,
    context_filter,
    jobs,
    life_os,
    misc,
    sessions,
    system_map,
    tokens,
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
# icons surviving for a year if we ever do swap them. .svg (the brand
# icons) is also ?v=-stamped client-side (issue #372), so the day cap
# only bounds an unstamped fetch; without it Safari's heuristic cache
# held the pre-#361 icons past the deploy.
_DAY_CACHE_SUFFIXES = {".webmanifest", ".png", ".ico", ".svg"}


class _VersionedStatic(StaticFiles):
    """Static mount that stamps Cache-Control + rewrites JS imports.

    JS files get their ``import './foo.js'`` calls rewritten to
    ``import './foo.js?v=<hash>'`` at serve time. Hashed assets get
    a year-long immutable cache; icons and manifest get a day;
    anything else falls back to defaults.
    """

    def __init__(self, *, directory: str, asset_hashes: Dict[str, str]) -> None:
        super().__init__(directory=directory)
        self._asset_hashes = asset_hashes
        self._static_dir = Path(directory)

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
            try:
                rel_parent = path.resolve().relative_to(self._static_dir.resolve()).parent
            except ValueError:
                rel_parent = Path(".")
            from_dir = "" if rel_parent == Path(".") else rel_parent.as_posix()
            rewritten = rewrite_js_imports(body, self._asset_hashes, from_dir)
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


async def _reconcile_orphan_mirror_windows(app: FastAPI) -> None:
    """On boot, close Edge mirror windows no live session backs (issue #199).

    The in-memory HWND registry (``src.launcher._mirror_hwnds``) is dropped
    on every webapp restart, so mirrors opened before the restart can no
    longer be closed by sid and pile up on the desktop. Reconcile them
    against the session-host's live list — but only when that list is
    *reliable*: a failed lookup means we can't tell live from orphan, so we
    skip rather than risk closing a live session's window.

    The sweep is machine-global (``EnumWindows`` over the whole desktop), so a
    *disposable* webapp — the e2e / verify-before-ship gate's autoboot instance,
    pointed at an empty disposable session-host — would see an empty live list
    and ``WM_CLOSE`` every real ``app-launcher-mirror-*`` window on the desktop,
    killing the user's live session mirrors while the sessions survive headless
    on the real ``:8446`` (issue #278). ``#260`` isolated the session-host but
    left this window sweep machine-wide. Only the canonical instance owns the
    desktop, so a disposable instance — identified by the
    ``LAUNCHER_SESSION_HOST_PORT`` override, which is set *only* by autoboot and
    never in production — skips the sweep entirely.
    """
    if os.environ.get(SESSION_HOST_PORT_ENV, "").strip():
        _log.debug(
            "ℹ️ orphan mirror reconcile skipped — disposable instance "
            "(%s set, e.g. e2e/verify autoboot); the machine-global sweep must "
            "not close the canonical instance's desktop windows (#278)",
            SESSION_HOST_PORT_ENV,
        )
        return
    cfg = getattr(app.state, "webapp_config", None)
    if cfg is None:
        return
    try:
        sessions_live = await asyncio.to_thread(
            session_client.list_sessions, cfg.session_host_port
        )
    except session_client.SessionHostError as exc:
        _log.debug(
            "ℹ️ orphan mirror reconcile skipped — session-host unreachable: %s",
            exc,
        )
        return
    live_sids = [
        str(s.get("session_id")) for s in sessions_live if s.get("session_id")
    ]
    try:
        closed = await asyncio.to_thread(
            launcher.close_orphan_mirror_windows, live_sids
        )
    except Exception as exc:  # noqa: BLE001
        _log.debug("ℹ️ orphan mirror reconcile failed: %s", exc)
        return
    if closed:
        _log.info("🧹 reconciled %d orphaned mirror window(s) on startup", closed)


# --- Jobs missed-fire coverage tick (issue #697) -------------------------
# First scan is delayed so it never competes with boot; then one scan per
# interval. The whole point of this tick is that it does NOT depend on the
# Jobs tab being open — the two real incidents (`config-map` / `sota-watch`
# registered no task at all) went unnoticed for weeks precisely because
# nothing looked when nobody was looking.
_COVERAGE_FIRST_DELAY_SECONDS = 120.0


def _coverage_interval_minutes(cfg: object) -> float:
    """The configured scan interval, defensively coerced.

    A config object without the key (an older ``webapp_config.json``, or a
    stub in a test) reads as the schema default rather than raising inside
    the lifespan.
    """
    try:
        return float(getattr(cfg, "jobs_coverage_interval_minutes", 60) or 0)
    except (TypeError, ValueError):
        return 0.0


async def _coverage_tick(app: FastAPI) -> None:
    """Periodically scan every job's schedule coverage and alert on breaks.

    Skipped entirely on a *disposable* instance (the e2e / verify-before-ship
    autoboot webapp, identified by ``LAUNCHER_SESSION_HOST_PORT`` exactly as
    the mirror-window sweep above is): a throwaway instance pointed at a
    scratch config must never push a real alert to the user's phone.

    Every scan is wrapped by :func:`src.jobs_coverage.check_and_alert`, which
    never raises — a wedged Task Scheduler or an unreachable Telegram must
    not take the webapp's lifespan down.
    """
    from src import jobs_coverage

    cfg = getattr(app.state, "webapp_config", None)
    if cfg is None:
        return
    interval = max(60.0, _coverage_interval_minutes(cfg) * 60.0)
    await asyncio.sleep(_COVERAGE_FIRST_DELAY_SECONDS)
    while True:
        alerted = await asyncio.to_thread(jobs_coverage.check_and_alert, cfg)
        if alerted:
            _log.warning(
                "🕳️ coverage alerts pushed for %d job(s): %s",
                len(alerted),
                ", ".join(alerted),
            )
        await asyncio.sleep(interval)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    await _reconcile_orphan_mirror_windows(app)
    coverage_task = None
    cfg = getattr(app.state, "webapp_config", None)
    disposable = bool(os.environ.get(SESSION_HOST_PORT_ENV, "").strip())
    if cfg is not None and not disposable and _coverage_interval_minutes(cfg) > 0:
        coverage_task = asyncio.create_task(_coverage_tick(app))
    elif disposable:
        _log.debug(
            "ℹ️ jobs coverage tick skipped — disposable instance (%s set)",
            SESSION_HOST_PORT_ENV,
        )
    try:
        yield
    finally:
        if coverage_task is not None:
            coverage_task.cancel()
            try:
                await coverage_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


def create_app() -> FastAPI:
    app_config = load_app_config()
    webapp_cfg = load_webapp_config()

    auth.ensure_log_handler()
    ensure_slow_log_handler()

    app = FastAPI(
        title="Launcher",
        version="0.1.0",
        lifespan=_lifespan,
    )

    app.add_middleware(
        BearerTokenMiddleware,
        get_config=lambda: app.state.webapp_config,
    )
    # Added last → outermost, so the timing covers the whole stack including
    # the auth middleware (issue #386).
    app.add_middleware(SlowRequestLogMiddleware)

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
    app.include_router(context_filter.router)
    app.include_router(tokens.router)
    app.include_router(apps.router)
    app.include_router(jobs.router)
    app.include_router(sessions.router)
    app.include_router(claude_code.router)
    app.include_router(life_os.router)
    app.include_router(board.router)
    app.include_router(system_map.router)
    app.include_router(webauthn.router)

    return app


# Module-level app for `uvicorn app.webapp.server:app`.
app = create_app()
