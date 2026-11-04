"""The hub's per-day, per-agent token history, read over loopback.

The hub owns the accounting (``GET /admin/api/code/usage/history``); the
launcher only reads what it already computed. The hub answers from its cache at
once, so a read never waits on a rebuild; ``refresh`` asks it to start one.
"""

from __future__ import annotations

from typing import Any, Dict

from src.clients import _loopback_http

_TIMEOUT = 5.0


class HubTokenHistoryError(_loopback_http.LoopbackError):
    """The hub is unreachable, has no such route, or answered with something unreadable."""


def history_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/admin/api/code/usage/history"


def fetch(base_url: str, first: str, last: str, *, refresh: bool = False) -> Dict[str, Any]:
    """The hub's history for ``first``..``last`` (UTC days, ``YYYY-MM-DD``). Blocking."""
    params: Dict[str, str] = {"from": first, "to": last}
    if refresh:
        params["refresh"] = "true"
    body = _loopback_http.request(
        "GET", history_url(base_url), error=HubTokenHistoryError, service="local-llm-hub",
        timeout=_TIMEOUT, allow_empty=False, params=params,
    )
    if not isinstance(body, dict) or not isinstance(body.get("days"), list) or not isinstance(body.get("cache"), dict):
        raise HubTokenHistoryError("local-llm-hub answered the token history with an unknown shape", status=502)
    return body
