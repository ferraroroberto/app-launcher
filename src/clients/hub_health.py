"""The PC health verdict from local-llm-hub, for the Code header's chip.

The hub owns the measurement, thresholds and alerts (its health watch samples
leaks and pressure on this machine). The launcher only reads
``GET /admin/api/machines/health`` over loopback and shows the verdict; it
never judges the machine itself.

**Unknown is its own state.** A hub that is not configured, does not answer,
answers with something that is not a verdict, or itself reports ``unknown``
reads ``unknown`` with a ``why``; none of them ever reads ``ok``.

The hub serves the answer from memory and re-samples every ten minutes, so a
short cache here keeps the page's poll from reaching it on every tick. A
failed read is cached for less time than a good one, so a hub that comes back
is noticed quickly without a down hub costing a connect timeout per poll.

Transitions into and out of ``unknown`` leave one breadcrumb each on the
``launcher.slowreq`` logger: the live webapp's own stdout is discarded by the
tray, and that logger persists to ``webapp/slow-requests.log``.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

from src.clients import _loopback_http

breadcrumbs = logging.getLogger("launcher.slowreq")

VERDICTS = ("ok", "watch", "act")
VERDICT_UNKNOWN = "unknown"

OK_CACHE_SECONDS = 30.0
FAILED_CACHE_SECONDS = 10.0
_TIMEOUT = 3.0
# A reason or fix is one line for a chip and a sheet row; the hub's own text
# is short, this only keeps a surprising payload from filling the sheet.
_MAX_TEXT = 240


class HubHealthError(_loopback_http.LoopbackError):
    """Raised when the hub is unreachable or answers with an error."""


_lock = threading.Lock()
_cached: Optional[Dict[str, Any]] = None
_cached_until = 0.0
_was_unknown = False


def _unknown(why: str, reason: Optional[str] = None) -> Dict[str, Any]:
    return {"verdict": VERDICT_UNKNOWN, "why": why, "reason": reason, "fix": None}


def _text(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    return " ".join(value.split())[:_MAX_TEXT]


def parse(body: Any) -> Dict[str, Any]:
    """The hub's payload as the launcher shows it. Anything that is not a
    recognised verdict is ``unknown``, never ``ok``."""
    if not isinstance(body, dict):
        return _unknown("unreadable")
    verdict = body.get("verdict")
    if verdict in VERDICTS:
        return {
            "verdict": verdict,
            "why": None,
            "reason": _text(body.get("reason")),
            "fix": _text(body.get("fix")),
        }
    if verdict == VERDICT_UNKNOWN:
        return _unknown("hub", _text(body.get("reason")))
    return _unknown("unreadable")


def health_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/admin/api/machines/health"


def _fetch(base_url: str) -> Dict[str, Any]:
    try:
        body = _loopback_http.request(
            "GET", health_url(base_url), error=HubHealthError, service="local-llm-hub",
            timeout=_TIMEOUT, allow_empty=False,
        )
    except HubHealthError as exc:
        return _unknown("unreachable", str(exc))
    return parse(body)


def _note_transition(reading: Dict[str, Any]) -> None:
    global _was_unknown
    now_unknown = reading["verdict"] == VERDICT_UNKNOWN
    if now_unknown and not _was_unknown:
        breadcrumbs.info("⚠️ hub health unknown (%s): %s", reading.get("why"), reading.get("reason") or "no detail")
    elif _was_unknown and not now_unknown:
        breadcrumbs.info("✅ hub health readable again: %s", reading["verdict"])
    _was_unknown = now_unknown


def read(base_url: str, now: Optional[float] = None) -> Dict[str, Any]:
    """The cached verdict, fetched when the cache is cold. Blocking."""
    global _cached, _cached_until
    base = (base_url or "").strip()
    if not base:
        return _unknown("disabled")
    now = time.monotonic() if now is None else now
    with _lock:
        if _cached is not None and now < _cached_until:
            return dict(_cached)
    reading = _fetch(base)
    ttl = FAILED_CACHE_SECONDS if reading["verdict"] == VERDICT_UNKNOWN else OK_CACHE_SECONDS
    with _lock:
        _note_transition(reading)
        _cached = reading
        _cached_until = now + ttl
    return dict(reading)


def invalidate() -> None:
    """Drop the cached verdict so the next read asks the hub again."""
    global _cached, _cached_until
    with _lock:
        _cached = None
        _cached_until = 0.0


def reset() -> None:
    """Drop all state. Tests only."""
    global _cached, _cached_until, _was_unknown
    with _lock:
        _cached = None
        _cached_until = 0.0
        _was_unknown = False
