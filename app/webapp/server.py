"""FastAPI webapp — phone-first launcher hub.

Routes are split across `app/webapp/routers/`, one module per family; read
the module for its surface. `docs/architecture.mmd` names every mounted router
(a test keeps that list current), so this docstring keeps no route table of its own.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from app.webapp import (
    channel_heal,
    chief_doorbell,
    chief_fallback,
    loop_lag,
    quota_cache,
    relay_idle_close,
)
from app.webapp.observability import (
    ensure_autostart_log_handler,
    ensure_doorbell_log_handler,
    ensure_slow_log_handler,
    ensure_warning_log_handler,
    request_timing_options,
)
from app.webapp.restart_targets import build_restart_router
from app.webapp.route_pools import (
    APP_PROBES,
    JOBS_FILES,
    OUTBOUND,
    SESSION_FILES,
    SESSION_HOST,
    run_on,
)
from app.webapp.routers import (
    apps,
    apps_perf,
    auth,
    board,
    claude_code,
    config,
    context_filter,
    desktop_screen,
    documents,
    fleet_scope,
    inspiration,
    jobs,
    life_os,
    life_os_channels,
    misc,
    perf_timings,
    progress,
    screen_time,
    session_screen,
    session_transcript,
    sessions,
    tokens,
    webauthn,
)
from app.webapp.routers._helpers import PROJECT_ROOT, STATIC_DIR
from app.webapp.static_trim import trim_js
from src import (
    app_runtime,
    instance_role,
    process_auto_restart,
    system_load,
)
from src.app_config import load_app_config
from src.auth.webauthn_gate import WebAuthnGate
from src.board import state_reconcile
from src.chief import machine_critical
from src.clients import session as session_client
from src.compression import StreamSafeGZipMiddleware
from src.desktop import prevent_lock
from src.jobs import snapshot as jobs_snapshot
from src.launching import launcher
from src.middleware import BearerTokenMiddleware
from src.middleware.unreadable_config import guard_unreadable_config
from src.ports import listeners as port_listeners
from src.request_timing import RequestTimingMiddleware, default_executor
from src.runtime_data import runtime_db_path
from src.static_bundles.js_bundle import BUNDLE_DIR, ENTRY, BundleError, build_js_bundle
from src.static_versioning import (
    compute_asset_hashes,
    fleet_hash_of,
    rewrite_js_imports,
)
from src.stylesheet_bundle import BUNDLE_RELPATH, build_bundle
from src.usage_log import usage_router
from src.webapp_config import load_webapp_config

_log = logging.getLogger(__name__)
# The lifespan's Telegram autostart lines: a child so the file handler
# takes these and not every other INFO this module logs.
_autostart_log = logging.getLogger(f"{__name__}.autostart")

_LONG_CACHE = "public, max-age=31536000, immutable"
_DAY_CACHE = "public, max-age=86400"
# Suffixes that get the year-long immutable cache. They go through the
# JS-import rewrite if .js; otherwise served as-is with the long header.
_HASHED_SUFFIXES = {".js", ".css"}
# Lightly cached (a day) — these change rarely but we don't want stale
# icons surviving for a year if we ever do swap them. .svg (the brand
# icons) is also ?v=-stamped client-side, so the day cap
# only bounds an unstamped fetch; without it Safari's heuristic cache
# held the previous icons past the deploy.
_DAY_CACHE_SUFFIXES = {".webmanifest", ".png", ".ico", ".svg"}
# Third-party bundles under static/: served as they are on disk.
_VENDOR_DIRS = ("vendor",)


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
        self._js_cache: Dict[str, Tuple[Tuple[int, int], str]] = {}

    def _served_js(self, path: Path, stat_result: os.stat_result) -> str:
        """The module as sent: comments stripped (vendored bundles are already
        minified and keep their licence text), imports stamped. Kept per file
        until it changes on disk, so a repeat request does no scan."""
        signature = (stat_result.st_mtime_ns, stat_result.st_size)
        cached = self._js_cache.get(str(path))
        if cached is not None and cached[0] == signature:
            return cached[1]
        body = path.read_text(encoding="utf-8")
        try:
            rel_parent = path.resolve().relative_to(self._static_dir.resolve()).parent
        except ValueError:
            rel_parent = Path(".")
        if rel_parent.parts[:1] != _VENDOR_DIRS:
            body = trim_js(body)
        from_dir = "" if rel_parent == Path(".") else rel_parent.as_posix()
        served = rewrite_js_imports(body, self._asset_hashes, from_dir)
        self._js_cache[str(path)] = (signature, served)
        return served

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
                body = self._served_js(path, stat_result)
            except OSError:
                return super().file_response(full_path, stat_result, scope, status_code)
            media_type, _ = mimetypes.guess_type(str(path))
            return Response(
                content=body,
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


def _load_stylesheet_bundle(app: FastAPI, asset_hashes: Dict[str, str]) -> Optional[str]:
    """Build the one-response stylesheet and register its route, or ``None``.

    The route is added ahead of the ``/static`` mount, which would otherwise
    answer ``/static/bundle.css`` with a 404. The bundle takes the fleet hash
    like every other asset so the index stamps its URL. On ``None`` the index
    keeps its separate links, so a failed build costs speed, not styling.
    """
    if not asset_hashes:
        return None
    try:
        bundle = build_bundle(STATIC_DIR)
    except OSError:
        _log.exception("❌ could not build the stylesheet bundle; serving separate sheets")
        return None
    if bundle is None:
        return None
    asset_hashes[BUNDLE_RELPATH] = app.state.asset_fleet_hash

    def _serve_bundle() -> Response:
        return Response(bundle, media_type="text/css", headers={"Cache-Control": _LONG_CACHE})

    app.add_api_route(f"/static/{BUNDLE_RELPATH}", _serve_bundle, include_in_schema=False)
    _log.info("ℹ️ Stylesheets bundled into /static/%s (%d bytes)", BUNDLE_RELPATH, len(bundle))
    return bundle


def _start_js_bundle_build(app: FastAPI, asset_hashes: Dict[str, str]) -> Optional[threading.Thread]:
    """Register the bundle route and build the ES-module bundle off the start path.

    A cold esbuild run takes up to ``BUILD_TIMEOUT_S``, longer than the tray's
    readiness deadline, so ``create_app`` must not wait for it. Until the build
    lands ``app.state.js_bundle`` stays ``None`` and the index links ``main.js``
    and its modules separately; a failed build leaves it ``None`` for good.
    Publishing is ordered (asset hash first, bundle last) so a page that links
    the bundle always finds its files. Returns the build thread, or ``None``
    when there is nothing to bundle.
    """
    app.state.js_bundle = None
    if not asset_hashes:
        return None
    fleet = app.state.asset_fleet_hash
    finished = threading.Event()

    def _serve_js_bundle(name: str) -> Response:
        bundle = app.state.js_bundle
        if bundle is None:
            if finished.is_set():
                return Response(status_code=404)
            # A page from the previous process can ask for a chunk while the
            # build runs; a retryable answer, never cached, keeps it from
            # reading as a missing file.
            return Response(status_code=503, headers={"Retry-After": "2", "Cache-Control": "no-store"})
        if name not in bundle:
            return Response(status_code=404)
        body, media_type = bundle[name]
        return Response(body, media_type=media_type, headers={"Cache-Control": _LONG_CACHE})

    app.add_api_route(f"/static/{BUNDLE_DIR}/{{name}}", _serve_js_bundle, include_in_schema=False)

    def _build() -> None:
        try:
            _publish()
        except (BundleError, OSError, ValueError) as exc:
            _log.warning("⚠️ could not bundle the ES modules; serving them separately: %s", exc)
        except Exception:  # noqa: BLE001 - a thread's escape would only reach stderr
            _log.exception("❌ ES-module bundle build crashed; serving the modules separately")
        finally:
            finished.set()

    def _publish() -> None:
        started = time.monotonic()
        bundle = build_js_bundle(STATIC_DIR, PROJECT_ROOT / "webapp" / BUNDLE_DIR, fleet)
        if bundle is None:
            return
        asset_hashes[f"{BUNDLE_DIR}/{ENTRY}"] = fleet
        app.state.js_bundle = bundle
        size = sum(len(body) for name, (body, _) in bundle.items() if name.endswith(".js"))
        _log.info(
            "ℹ️ ES modules bundled into /static/%s/ (%d files, %d bytes of JS) in %.1f s",
            BUNDLE_DIR, len(bundle), size, time.monotonic() - started,
        )

    thread = threading.Thread(target=_build, name="js-bundle-build", daemon=True)
    thread.start()
    return thread


async def _reconcile_orphan_mirror_windows(app: FastAPI) -> None:
    """On boot, close Edge mirror windows no live session backs.

    The in-memory HWND registry (``src.launching.launcher._mirror_hwnds``) is dropped
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
    on the real ``:8446``. The disposable instance isolates the session-host but
    not this window sweep, which is machine-wide. Only the canonical instance owns the
    desktop, so a disposable instance (``instance_role.is_disposable``) skips
    the sweep entirely.
    """
    if instance_role.is_disposable():
        _log.debug(
            "ℹ️ orphan mirror reconcile skipped — disposable instance "
            "(e2e/verify autoboot); the machine-global sweep must "
            "not close the canonical instance's desktop windows (#278)"
        )
        return
    cfg = getattr(app.state, "webapp_config", None)
    if cfg is None:
        return
    try:
        sessions_live = await run_on(
            SESSION_HOST, session_client.list_sessions, cfg.session_host_port
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
        closed = await run_on(
            OUTBOUND, launcher.close_orphan_mirror_windows, live_sids
        )
    except Exception as exc:  # noqa: BLE001
        _log.debug("ℹ️ orphan mirror reconcile failed: %s", exc)
        return
    if closed:
        _log.info("🧹 reconciled %d orphaned mirror window(s) on startup", closed)


# --- Jobs stranded-run reap + missed-fire coverage tick -----
# First scan is delayed so it never competes with boot; then one scan per
# interval. The whole point of this tick is that it does NOT depend on the
# Jobs tab being open — the two real missed-fire incidents (`config-map` /
# `sota-watch` registered no task at all) went unnoticed for weeks, and a
# stranded run went unreaped for over two months, precisely because
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


def _publish_script_missing(cfg: object) -> None:
    """Tell the chief about any job whose script moved (once per job per day)."""
    from src.chief import script_missing
    from src.jobs.config import load_jobs

    script_missing.observe(cfg, load_jobs().jobs)


async def _coverage_cycle(cfg: object) -> None:
    """One sweep: stranded-run reap then missed-fire coverage.

    Reap runs first — :func:`src.jobs.reap.reap_stranded_runs_all` is
    otherwise lazy-on-read (only ``GET /api/jobs`` or the Board trigger it),
    so a stranded record from a checkout nobody polls could sit
    un-finalised, and its failure alert un-sent, indefinitely. Reaping first
    also means coverage's behavioural half sees a freshly-terminal record
    rather than a stale "running" one.

    Both halves never raise — a wedged Task Scheduler, a bad job's history,
    or an unreachable Telegram must not take the webapp's lifespan down.
    """
    from src.jobs import coverage as jobs_coverage
    from src.jobs.reap import reap_stranded_runs_all

    reaped = await run_on(JOBS_FILES, reap_stranded_runs_all)
    if reaped:
        _log.warning(
            "🧟 swept %d stranded run(s) across %d job(s): %s",
            sum(len(v) for v in reaped.values()),
            len(reaped),
            ", ".join(reaped),
        )
    await run_on(JOBS_FILES, _publish_script_missing, cfg)
    alerted = await run_on(OUTBOUND, jobs_coverage.check_and_alert, cfg)
    if alerted:
        _log.warning(
            "🕳️ coverage alerts pushed for %d job(s): %s",
            len(alerted),
            ", ".join(alerted),
        )


async def _coverage_tick(app: FastAPI) -> None:
    """Periodically reap stranded runs and scan schedule coverage.

    Started only on the **canonical** instance
    (:func:`src.instance_role.canonical_instance`): a non-canonical checkout
    carries the real config and real credentials but derives coverage (and
    reaps) from its own empty ``webapp/jobs/``, so it must never push a real
    alert to the user's phone. That covers the e2e / verify-before-ship
    autoboot webapp — the ``LAUNCHER_SESSION_HOST_PORT`` case
    and the same marker the mirror-window sweep above uses —
    *and* the git worktree that actually pushed three false alerts.
    """
    cfg = getattr(app.state, "webapp_config", None)
    if cfg is None:
        return
    interval = max(60.0, _coverage_interval_minutes(cfg) * 60.0)
    await asyncio.sleep(_COVERAGE_FIRST_DELAY_SECONDS)
    while True:
        await _coverage_cycle(cfg)
        await asyncio.sleep(interval)


# --- webapp/sessions retention sweep ------------------------
# Once a day, off the event loop. The first sweep waits well past boot so it
# never competes with the restart it follows.
_RETENTION_FIRST_DELAY_SECONDS = 600.0
_RETENTION_INTERVAL_SECONDS = 24 * 3600.0


def _session_retention_days(cfg: object) -> int:
    """The configured window in days; ``0`` (delete nothing) when unset or unknown.

    Unlike the coverage interval, a config object *without* the key reads as
    off, not as the schema default: this tick deletes, so an absent setting
    must never arm it. A real :class:`~src.webapp_config.WebappConfig` always
    carries the field, but the owner's window is unknown when the file could not
    be read (the values are defaults) or its retention value is no window.
    """
    error = getattr(cfg, "load_error", "") or getattr(cfg, "retention_error", "")
    if error:
        _log.warning(
            "⚠️ session retention off: %s. Nothing is deleted until the file "
            "is fixed and the app restarted.",
            error,
        )
        return 0
    try:
        return max(0, int(getattr(cfg, "session_retention_days", 0) or 0))
    except (TypeError, ValueError):
        return 0


async def _session_retention_tick(app: FastAPI) -> None:
    """Sweep expired ``webapp/sessions`` files once a day.

    Canonical-instance only, like the coverage tick: a disposable gate webapp
    shares this checkout's ``webapp/sessions`` but talks to its own empty
    session-host, so its "live" list would protect none of the real sessions.
    The config is re-read each cycle so a Settings save that swaps
    ``app.state.webapp_config`` is honoured without a restart.
    """
    from src import session_retention

    await asyncio.sleep(_RETENTION_FIRST_DELAY_SECONDS)
    while True:
        cfg = getattr(app.state, "webapp_config", None)
        days = _session_retention_days(cfg)
        if cfg is not None and days > 0:
            try:
                await run_on(
                    SESSION_FILES, session_retention.run_scheduled_sweep,
                    cfg.session_host_port,
                    days,
                )
            except Exception:  # noqa: BLE001 — never take the lifespan down
                _log.exception("❌ session retention sweep failed")
        await asyncio.sleep(_RETENTION_INTERVAL_SECONDS)


def _autostart_channel_sessions_on(cfg: object) -> bool:
    """Whether the Telegram autostart is switched on. Only a real
    ``True`` arms it: it launches sessions, so a config without the key (a
    stub) or a non-boolean must read as off, like the retention sweep."""
    return getattr(cfg, "autostart_channel_sessions", False) is True


LISTENER_WAIT_SECONDS = 120.0
LISTENER_POLL_SECONDS = 1.0


async def _owns_listener(port: int) -> bool:
    """Whether this process holds the LISTEN socket on ``port``, waiting for it
    to be bound (the lifespan runs before the server binds).

    ``False`` when another process holds it, or when it never bound or the
    socket table could not be read within ``LISTENER_WAIT_SECONDS``: a launch
    whose ownership is not confirmed would duplicate the owner's.
    """
    from src import diagnostics

    own = os.getpid()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + LISTENER_WAIT_SECONDS
    while True:
        pids = await run_on(APP_PROBES, diagnostics.listener_pids, port)
        if pids is not None and own in pids:
            return True
        if pids:
            _autostart_log.info(
                f"ℹ️ Telegram autostart: pid {own} skipped, :{port} is held by "
                f"pid {pids[0]}"
            )
            return False
        if loop.time() >= deadline:
            what = "never bound" if pids is not None else "could not be read"
            _autostart_log.warning(
                f"⚠️ Telegram autostart: pid {own} skipped, :{port} {what} "
                f"within {LISTENER_WAIT_SECONDS:.0f}s; ownership not confirmed"
            )
            return False
        await asyncio.sleep(LISTENER_POLL_SECONDS)


async def _channel_autostart(app: FastAPI) -> None:
    """Start the ready Telegram channel profiles once, at boot.

    Canonical-instance only: a disposable gate webapp has its own empty
    session-host, so its "already running" check would see none of the live
    bots and start a second listener on each one's token. Listener-owner
    only too: a second webapp process that lost the bind would otherwise
    race the owner's pass through the same check-then-spawn.
    """
    from app.webapp.routers.life_os_channels import autostart_channel_sessions

    cfg = getattr(app.state, "webapp_config", None)
    if cfg is None:
        return
    if not await _owns_listener(cfg.port):
        return
    await autostart_channel_sessions(
        cfg,
        profiles_path=getattr(app.state, "channel_profiles_path", None),
        installed_plugins_path=getattr(app.state, "installed_plugins_path", None),
    )


def _canonical_loops(app: FastAPI) -> List[asyncio.Task]:
    """The always-on lifespan loops only the canonical checkout may run: each
    reads its own Settings switch every beat, so a save arms it live."""
    tasks: List[asyncio.Task] = []
    # Always started: it re-reads the chief_doorbell toggle every tick and
    # does nothing while it is off, so a Settings save arms it live.
    tasks.append(asyncio.create_task(chief_doorbell.tick_forever(app)))
    # Always started, independent of the doorbell switch: it reads
    # chief_fallback_minutes every check and does nothing at 0.
    tasks.append(asyncio.create_task(chief_fallback.tick_forever(app)))
    # Always started: it reads the night auto-restart switch every tick and does
    # nothing while it is off. Canonical instance only, so a disposable gate
    # webapp never restarts a real process.
    tasks.append(asyncio.create_task(process_auto_restart.tick_forever(app)))
    # Always started: it reads the Prevent lock switch every beat and holds
    # and sends nothing while it is off. Canonical instance only, so a
    # disposable gate webapp never moves the real pointer.
    tasks.append(asyncio.create_task(prevent_lock.tick_forever(app)))
    # The hub's machine verdict at `act` on ports or commit becomes one
    # chief mailbox event per incident. Canonical instance only, so a gate
    # webapp never writes the real mailbox.
    tasks.append(asyncio.create_task(machine_critical.tick_forever(app)))
    # Hook-state rows whose process is gone become history. Canonical only:
    # it rewrites the real state file against the real session-host.
    tasks.append(asyncio.create_task(state_reconcile.tick_forever(
        lambda: getattr(app.state, "webapp_config", None)
    )))
    # Always started: it reads each bot's idle_close_minutes every tick and
    # touches only what the relay reopened, so a settings save applies live.
    tasks.append(asyncio.create_task(relay_idle_close.tick_forever(app)))
    # A running bot whose plugin server is gone is restarted once, then
    # flagged for the owner. Touches only sessions that are already running.
    # Listener-owner only, like the autostart: two webapp processes must not
    # both restart one bot.
    tasks.append(asyncio.create_task(
        channel_heal.tick_forever(app, owns_listener=_owns_listener)
    ))
    # The Projects tiles' ahead/behind: a slow fetch round while a page
    # polls git status. Canonical only, so a gate webapp stays offline.
    tasks.append(asyncio.create_task(claude_code.fetch_tick_forever(app)))
    return tasks


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # The loop's own default executor, swapped for one that reports queue and
    # worker time to the request that submitted the work (src/request_timing.py).
    asyncio.get_running_loop().set_default_executor(default_executor())
    await _reconcile_orphan_mirror_windows(app)
    # Launched apps outlive a launcher restart; take the previous launcher's
    # records back now so the line naming any that died lands at boot.
    await run_on(APP_PROBES, app_runtime.adopt_previous)
    # The jobs runtime snapshot behind /api/jobs and /api/board. It
    # only walks while those polls are asking, so every instance runs it:
    # the reads it serves happen on every instance too.
    tasks = [asyncio.create_task(jobs_snapshot.tick_forever())]
    # The header's CPU reading: one shared sampler, awake only while
    # a page polls /api/system/load.
    tasks.append(asyncio.create_task(system_load.tick_forever()))
    # The Apps tab's port listeners: one shared scan, awake only while the
    # list is read; /api/ports/probe never scans.
    tasks.append(asyncio.create_task(port_listeners.SAMPLER.tick_forever()))
    # The snapshot behind /api/version, rebuilt only while that route is polled.
    tasks.append(asyncio.create_task(misc.version_cache(app).tick_forever()))
    # The Settings card's channel list, rebuilt only while it is polled.
    tasks.append(asyncio.create_task(life_os_channels.channels_cache(app).tick_forever()))
    # The context-filter card's stats, parsed only while that route is polled.
    tasks.append(asyncio.create_task(context_filter.context_filter_cache(app).tick_forever()))
    # The usage meters' quota rows, re-read only while /api/rate-limits is polled.
    tasks.append(asyncio.create_task(quota_cache.rate_limits_cache(app).tick_forever()))
    # Re-reads the terminal_timing_log switch each round, so a config save arms it live.
    tasks.append(asyncio.create_task(loop_lag.tick_forever(
        lambda: getattr(getattr(app.state, "webapp_config", None), "terminal_timing_log", False)
    )))
    cfg = getattr(app.state, "webapp_config", None)
    canonical, role_reason = instance_role.canonical_instance()
    if cfg is not None and canonical:
        if _coverage_interval_minutes(cfg) > 0:
            tasks.append(asyncio.create_task(_coverage_tick(app)))
        if _session_retention_days(cfg) > 0:
            tasks.append(asyncio.create_task(_session_retention_tick(app)))
        tasks.extend(_canonical_loops(app))
        if _autostart_channel_sessions_on(cfg):
            tasks.append(asyncio.create_task(_channel_autostart(app)))
        else:
            _autostart_log.info(
                "ℹ️ Telegram autostart is off; no channel session started"
            )
    elif not canonical:
        # The autoboot case is expected on every gate run, so it stays quiet;
        # any *other* non-canonical instance is a surprise worth finding in
        # the log after the phone buzzes.
        emit, mark = (
            (_autostart_log.debug, "ℹ️")
            if role_reason == instance_role.REASON_DISPOSABLE
            else (_autostart_log.warning, "⚠️")
        )
        emit(
            "%s jobs coverage + session retention ticks, the chief doorbell, "
            "the relay idle close and Telegram autostart skipped — non-canonical instance (%s, "
            "root=%s); only the canonical checkout may alert, reap, ring or "
            "launch (#736, #902, #1542, #1645)",
            mark,
            role_reason,
            instance_role.PROJECT_ROOT,
        )
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


def _budget_overrides(app: FastAPI):
    """The config's ``request_budgets``, read per request so a save applies live."""
    return lambda: getattr(getattr(app.state, "webapp_config", None), "request_budgets", None)


def create_app() -> FastAPI:
    app_config = load_app_config()
    webapp_cfg = load_webapp_config()

    auth.ensure_log_handler()
    ensure_slow_log_handler()
    ensure_autostart_log_handler(_autostart_log, life_os_channels.logger, relay_idle_close.logger)
    ensure_doorbell_log_handler(chief_doorbell.logger)
    ensure_warning_log_handler()

    app = FastAPI(
        title="Launcher",
        version="0.1.0",
        lifespan=_lifespan,
    )

    app.add_middleware(
        BearerTokenMiddleware,
        get_config=lambda: app.state.webapp_config,
    )
    guard_unreadable_config(app, webapp_cfg, _log)
    # Outside the auth middleware, so the timing covers it.
    app.add_middleware(RequestTimingMiddleware, **request_timing_options(_budget_overrides(app)))
    # Added last → outermost, so an auth failure or a bodyless 304 passes
    # through it unchanged. No bypass path: the TTS proxy is `audio/L16` and
    # the SSE stream is `text/event-stream`, both skipped by content type.
    app.add_middleware(StreamSafeGZipMiddleware, bypass_paths=())

    app.state.app_config = app_config
    app.state.webapp_config = webapp_cfg
    app.state.webauthn_gate = WebAuthnGate()
    app.state.login_lockout = auth.build_login_lockout(lambda: app.state.webapp_config)

    asset_hashes = compute_asset_hashes(STATIC_DIR)
    app.state.asset_hashes = asset_hashes
    app.state.asset_fleet_hash = fleet_hash_of(asset_hashes)
    if asset_hashes:
        _log.info(
            "ℹ️ Static assets stamped at fleet hash %s (%d files)",
            app.state.asset_fleet_hash,
            len(asset_hashes),
        )

    app.state.stylesheet_bundle = _load_stylesheet_bundle(app, asset_hashes)
    app.state.js_bundle_build = _start_js_bundle_build(app, asset_hashes)

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
    app.include_router(fleet_scope.router)
    app.include_router(inspiration.router)
    app.include_router(progress.router)
    app.include_router(documents.router)
    app.include_router(tokens.router)
    app.include_router(apps_perf.router)
    app.include_router(apps.router)
    app.include_router(jobs.router)
    app.include_router(sessions.router)
    app.include_router(claude_code.router)
    app.include_router(session_transcript.router)
    app.include_router(session_screen.router)
    app.include_router(desktop_screen.router)
    app.include_router(life_os.router)
    app.include_router(board.router)
    app.include_router(webauthn.router)
    # Local-only view/tap log. Behind the bearer-token middleware like
    # every /api route; static/usage.js hands the vendored client the bearer
    # header through its `headers` option.
    app.include_router(usage_router(
        runtime_db_path("app-launcher", "usage.sqlite3", create=True),
    ))
    # The Screen time sheet's Send to task-os: drives the same
    # src/usage/export.py the usage-export CLI and the nightly Job run.
    app.include_router(screen_time.router)
    # Real-phone load and slow-API timings (static/perf-timing.js).
    app.include_router(perf_timings.router)
    # The build readout's restart sheet: the app, and the session-host.
    app.include_router(build_restart_router(app))

    return app


# Module-level app for `uvicorn app.webapp.server:app`.
app = create_app()
