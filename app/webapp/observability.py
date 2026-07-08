"""Slow-request breadcrumbs for the webapp (issue #386).

The webapp runs under ``--log-level warning`` and the tray spawns it with
stdout/stderr DEVNULL'd, so when :8445 wedges there is zero evidence trail.
This middleware leaves one, in ``webapp/slow-requests.log``:

* one line per request slower than ``LAUNCHER_SLOW_REQUEST_S`` (default 3 s):
  method, path, status, elapsed, and how many other requests were still in
  flight when it finished;
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
import os
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)
slow_logger = logging.getLogger("launcher.slowreq")

_SLOW_LOG_PATH = (
    Path(__file__).resolve().parent.parent.parent / "webapp" / "slow-requests.log"
)

DEFAULT_SLOW_REQUEST_S = 3.0
# The SPA's boot legitimately fans out ~9 parallel fetches (observed in the
# e2e gate), so the floor sits well above that — a breadcrumb here must mean
# "abnormal", not "page loaded".
DEFAULT_INFLIGHT_WARN = 16
# Floor between two in-flight breadcrumbs, so a hammered server logs a
# heartbeat, not a flood.
_INFLIGHT_LOG_INTERVAL_S = 30.0


def ensure_slow_log_handler() -> None:
    """Attach the slow-requests.log file handler exactly once. Idempotent —
    safe to call from ``create_app()`` on every boot. Without a file handler
    the breadcrumbs would vanish into the tray's DEVNULL exactly when they
    matter."""
    if any(
        isinstance(h, logging.FileHandler)
        and Path(h.baseFilename).resolve() == _SLOW_LOG_PATH.resolve()
        for h in slow_logger.handlers
    ):
        return
    try:
        _SLOW_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(_SLOW_LOG_PATH, encoding="utf-8")
        fh.setLevel(logging.WARNING)
        fh.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        slow_logger.addHandler(fh)
        slow_logger.setLevel(logging.WARNING)
    except OSError as exc:
        logger.warning(f"⚠️  Could not open {_SLOW_LOG_PATH}: {exc}")


class SlowRequestLogMiddleware:
    """ASGI middleware: log slow requests + high in-flight counts."""

    def __init__(
        self,
        app,
        slow_s: Optional[float] = None,
        inflight_warn: Optional[int] = None,
    ) -> None:
        self.app = app
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
        self._maybe_log_inflight(start)

        status_holder = {"status": 0}

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message.get("status", 0)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            self._inflight.pop(rid, None)
            elapsed = time.monotonic() - start
            if elapsed >= self.slow_s:
                slow_logger.warning(
                    "🐢 slow request: %s %s → %s in %.1fs (%d still in flight)",
                    method,
                    path,
                    status_holder["status"] or "?",
                    elapsed,
                    len(self._inflight),
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
