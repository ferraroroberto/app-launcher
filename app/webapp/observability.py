"""Slow-request breadcrumbs for the webapp.

The webapp runs under ``--log-level warning`` and the tray spawns it with
stdout/stderr DEVNULL'd, so when :8445 wedges there is zero evidence trail.
This middleware leaves one, in ``webapp/slow-requests.log``:

* one line per request slower than ``LAUNCHER_SLOW_REQUEST_S`` (default 3 s):
  method, path, status, elapsed, and how many other requests were still in
  flight when it finished;
* one line per request over its class's budget (``src/request_timing.py``'s
  table: the phone's hot paths, the transcript tick and open, send and git
  status): "over budget" past the soft limit, "budget breach" past the hard
  one, so a regression is on disk before anyone notices it by feel;
* on every slow or over-budget line, where the time went: ``queue_ms`` (waiting
  for a pool thread), ``worker_ms`` (running on one), ``total_ms`` and the
  ``pool`` name, which is also what ``Server-Timing`` carries;
* a rate-limited in-flight line whenever a new request arrives while more
  than ``LAUNCHER_INFLIGHT_WARN`` (default 16) are already in flight — with
  the age + path of the oldest, enough to tell "event-loop blocked" from
  "handlers deadlocked" from "TLS/socket exhaustion" at the next wedge.

Pure ASGI (not ``BaseHTTPMiddleware``) so it adds no response buffering; it
only times ``http`` scopes — WebSocket sessions are long-lived by design and
would be noise.
"""

from __future__ import annotations

import itertools
import logging
import logging.handlers
import os
import time
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple

from src import request_timing, runtime_files

logger = logging.getLogger(__name__)
slow_logger = logging.getLogger("launcher.slowreq")

_SLOW_LOG_PATH = (
    Path(__file__).resolve().parent.parent.parent / "webapp" / "slow-requests.log"
)

_AUTOSTART_LOG_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "webapp"
    / "channel-autostart.log"
)

_DOORBELL_LOG_PATH = (
    Path(__file__).resolve().parent.parent.parent / "webapp" / "chief-doorbell.log"
)

_WARNINGS_LOG_PATH = (
    Path(__file__).resolve().parent.parent.parent / "webapp" / "webapp-warnings.log"
)
# A WARNING flood (a wedge logs one per stalled request) must not fill the
# disk: 2 MB x 3 backups keeps days of normal history.
_WARNINGS_LOG_MAX_BYTES = 2 * 1024 * 1024
_WARNINGS_LOG_BACKUPS = 3

DEFAULT_SLOW_REQUEST_S = 3.0
# The SPA's boot legitimately fans out ~9 parallel fetches (observed in the
# e2e gate), so the floor sits well above that — a breadcrumb here must mean
# "abnormal", not "page loaded".
DEFAULT_INFLIGHT_WARN = 16
# Floor between two in-flight breadcrumbs, so a hammered server logs a
# heartbeat, not a flood.
_INFLIGHT_LOG_INTERVAL_S = 30.0


def ensure_file_log_handler(
    path: Path,
    *loggers: logging.Logger,
    fmt: str = "%(asctime)s [%(levelname)s] %(message)s",
) -> None:
    """Attach an INFO ``FileHandler`` at ``path`` to every logger in
    ``loggers``, exactly once each. Idempotent per logger — safe to call
    from ``create_app()`` on every boot. Under the tray's ``pythonw`` there
    is no stderr, so a logger with no file handler of its own simply has no
    evidence trail; this is the one place that attaches one for auth.log,
    slow-requests.log and channel-autostart.log."""
    resolved = path.resolve()
    pending = [
        lg
        for lg in loggers
        if not any(
            isinstance(h, logging.FileHandler)
            and Path(h.baseFilename).resolve() == resolved
            for h in lg.handlers
        )
    ]
    if not pending:
        return
    try:
        runtime_files.make_dirs(path.parent)
        fh = runtime_files.file_handler(path, mode="a", encoding="utf-8")
    except OSError as exc:
        logger.warning(f"⚠️  Could not open {path}: {exc}")
        return
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter(fmt))
    for lg in pending:
        lg.addHandler(fh)
        lg.setLevel(logging.INFO)


def ensure_slow_log_handler() -> None:
    """Attach the slow-requests.log file handler exactly once. Idempotent —
    safe to call from ``create_app()`` on every boot. Without a file handler
    the breadcrumbs would vanish into the tray's DEVNULL exactly when they
    matter. INFO, so the hot polls' step timings land here too."""
    ensure_file_log_handler(_SLOW_LOG_PATH, slow_logger)


def ensure_autostart_log_handler(*targets: logging.Logger) -> None:
    """Send the Telegram autostart breadcrumbs to
    ``webapp/channel-autostart.log`` at INFO. Idempotent per logger —
    safe to call from ``create_app()`` on every boot. Under the tray's
    ``pythonw`` there is no stderr and these loggers have no file handler, so
    the "autostart is off" / per-profile started-or-skipped lines (and the
    unreachable-session-host warning, which falls to ``logging.lastResort``
    and its ``None`` stderr) would otherwise vanish. The relay idle close's
    lines join them, for the same reason. Pass these channel loggers only,
    never the root: other modules' INFO stays out."""
    ensure_file_log_handler(
        _AUTOSTART_LOG_PATH, *targets,
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def ensure_doorbell_log_handler(target: logging.Logger) -> None:
    """Send the chief doorbell's rings and skip reasons to
    ``webapp/chief-doorbell.log`` at INFO, for the same reason as the
    autostart log: under the tray's ``pythonw`` they would otherwise vanish."""
    ensure_file_log_handler(_DOORBELL_LOG_PATH, target)


def ensure_warning_log_handler() -> None:
    """Persist every WARNING+ record the webapp logs to the rotating
    ``webapp/webapp-warnings.log``. Idempotent — safe to call from
    ``create_app()`` on every boot. The tray spawns the webapp with stderr
    DEVNULL'd, so a warning from a module with no file handler of its own
    (the server's errors included, since hypercorn's logger propagates)
    would otherwise reach ``logging.lastResort`` and vanish. Attached to the
    root logger at WARNING only, leaving the root level untouched, so no
    module's INFO lands here."""
    root = logging.getLogger()
    resolved = _WARNINGS_LOG_PATH.resolve()
    if any(
        isinstance(h, logging.FileHandler)
        and Path(h.baseFilename).resolve() == resolved
        for h in root.handlers
    ):
        return
    try:
        _WARNINGS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            _WARNINGS_LOG_PATH,
            maxBytes=_WARNINGS_LOG_MAX_BYTES,
            backupCount=_WARNINGS_LOG_BACKUPS,
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning(f"⚠️  Could not open {_WARNINGS_LOG_PATH}: {exc}")
        return
    fh.setLevel(logging.WARNING)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root.addHandler(fh)


def _with_server_timing(
    message: dict, elapsed_s: float, timing: request_timing.RequestTiming
) -> dict:
    """The response-start message plus ``Server-Timing``: ``queue`` and
    ``worker`` (pool time, with the pool name) and ``app``, the server's own
    time to headers, so a client can subtract it from its wait and see the
    network round trip (``src/usage/perf_timings.py``), and a benchmark can
    tell queueing from work."""
    header = (b"server-timing", timing.server_timing(elapsed_s).encode("ascii"))
    return {**message, "headers": [*message.get("headers", []), header]}


class SlowRequestLogMiddleware:
    """ASGI middleware: log slow requests + high in-flight counts, and stamp
    ``Server-Timing`` on every ``/api/`` response."""

    def __init__(
        self,
        app,
        slow_s: Optional[float] = None,
        inflight_warn: Optional[int] = None,
        get_budget_overrides: Optional[
            Callable[[], Optional[Mapping[str, Sequence[float]]]]
        ] = None,
        stats: Optional[request_timing.RequestStats] = None,
    ) -> None:
        self.app = app
        # Read per request, so a config edit applies live.
        self._get_budget_overrides = get_budget_overrides
        self.stats = request_timing.STATS if stats is None else stats
        self.slow_s = (
            float(os.environ.get("LAUNCHER_SLOW_REQUEST_S", DEFAULT_SLOW_REQUEST_S))
            if slow_s is None
            else slow_s
        )
        self.inflight_warn = (
            int(os.environ.get("LAUNCHER_INFLIGHT_WARN", DEFAULT_INFLIGHT_WARN))
            if inflight_warn is None
            else inflight_warn
        )
        # request id → (method, path, start). Single event loop — no lock.
        self._inflight: Dict[int, Tuple[str, str, float]] = {}
        self._ids = itertools.count(1)
        self._last_inflight_log = 0.0

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "?")
        path = scope.get("path", "?")
        rid = next(self._ids)
        start = time.monotonic()
        self._inflight[rid] = (method, path, start)
        self.stats.entered(len(self._inflight))
        self._maybe_log_inflight(start)

        status_holder = {"status": 0}
        timing = request_timing.RequestTiming()
        token = request_timing.current.set(timing)

        timed = path.startswith("/api/")

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message.get("status", 0)
                if timed:
                    message = _with_server_timing(message, time.monotonic() - start, timing)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            request_timing.current.reset(token)
            self._inflight.pop(rid, None)
            self.stats.left(len(self._inflight))
            elapsed = time.monotonic() - start
            status = status_holder["status"] or "?"
            if elapsed >= self.slow_s:
                slow_logger.warning(
                    "🐢 slow request: %s %s → %s in %.1fs (%d still in flight) [%s]",
                    method,
                    path,
                    status,
                    elapsed,
                    len(self._inflight),
                    timing.log_fields(elapsed),
                )
            self._judge_budget(method, path, scope, status, elapsed, timing)

    def _judge_budget(self, method, path, scope, status, elapsed, timing) -> None:
        query = scope.get("query_string", b"").decode("latin-1")
        name = request_timing.classify(method, path, query)
        if name is None:
            return
        overrides = self._get_budget_overrides() if self._get_budget_overrides else None
        budget = request_timing.resolve_budgets(overrides)[name]
        verdict = request_timing.budget_verdict(budget, elapsed)
        self.stats.judged(name, verdict)
        if verdict == "breach":
            slow_logger.warning(
                "🚨 budget breach: %s %s (%s) → %s in %.2fs, hard limit %.2fs [%s]",
                method, path, name, status, elapsed, budget.hard_s, timing.log_fields(elapsed),
            )
        elif verdict == "over":
            slow_logger.warning(
                "⏱️ over budget: %s %s (%s) → %s in %.2fs, soft limit %.2fs [%s]",
                method, path, name, status, elapsed, budget.soft_s, timing.log_fields(elapsed),
            )

    def _maybe_log_inflight(self, now: float) -> None:
        count = len(self._inflight)
        if count <= self.inflight_warn:
            return
        if now - self._last_inflight_log < _INFLIGHT_LOG_INTERVAL_S:
            return
        self._last_inflight_log = now
        oldest = min(self._inflight.values(), key=lambda entry: entry[2])
        slow_logger.warning(
            "⚠️ %d requests in flight (threshold %d); oldest: %s %s, %.1fs old",
            count,
            self.inflight_warn,
            oldest[0],
            oldest[1],
            now - oldest[2],
        )
