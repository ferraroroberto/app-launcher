"""Thin HTTP client for the loopback PTY session-host.

The webapp owns all auth, Tailscale gating, and WebAuthn; it talks to the
session-host (``app/session_host/server.py``) purely over loopback. These
are blocking ``requests`` calls — webapp routes wrap them in
``asyncio.to_thread`` so the event loop never stalls. The WebSocket proxy
is handled separately in ``app/webapp/server.py``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import requests

from src import _loopback_http

logger = logging.getLogger(__name__)

_TIMEOUT = 8.0
# Spawning a PTY session is slow (cold pywinpty + claude --remote-control
# can take 10–20 s on a freshly booted box). Reuse of the 8 s default here
# was surfacing 'session-host unreachable' to the phone while the spawn was
# still in flight, prompting retries that stacked orphan sessions.
_CREATE_TIMEOUT = 45.0


def base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def ws_url(port: int, session_id: str, role: str = "phone") -> str:
    return f"ws://127.0.0.1:{port}/sessions/{session_id}/ws?role={role}"


class SessionHostError(_loopback_http.LoopbackError):
    """Raised when the session-host is unreachable or returns an error."""


def _request(method: str, port: int, path: str, *, timeout: float = _TIMEOUT, **kwargs) -> Any:
    return _loopback_http.request(
        method,
        base_url(port) + path,
        error=SessionHostError,
        service="session-host",
        timeout=timeout,
        **kwargs,
    )


def health(port: int) -> bool:
    try:
        resp = requests.get(base_url(port) + "/healthz", timeout=2.0)
        return resp.status_code == 200
    except requests.RequestException:
        return False


def create_session(
    port: int,
    project_dir: str,
    name: str,
    flags: str,
    kind: str = "pty",
    agent: str = "claude",
    rows: int = 40,
    cols: int = 120,
) -> Dict[str, Any]:
    return _request(
        "POST",
        port,
        "/sessions",
        timeout=_CREATE_TIMEOUT,
        json={
            "project_dir": project_dir,
            "name": name,
            "flags": flags,
            "kind": kind,
            "agent": agent,
            # Phone's real terminal size, so the PTY's first frame is the
            # right width for a ratatui TUI (issue #126).
            "rows": rows,
            "cols": cols,
        },
    )


def list_sessions(port: int) -> List[Dict[str, Any]]:
    data = _request("GET", port, "/sessions")
    return list(data.get("sessions") or [])


def get_session(port: int, session_id: str) -> Dict[str, Any]:
    return _request("GET", port, f"/sessions/{session_id}")


def send_input(port: int, session_id: str, data: str) -> Dict[str, Any]:
    return _request(
        "POST", port, f"/sessions/{session_id}/input", json={"data": data}
    )


def resize(port: int, session_id: str, rows: int, cols: int) -> Dict[str, Any]:
    return _request(
        "POST",
        port,
        f"/sessions/{session_id}/resize",
        json={"rows": rows, "cols": cols},
    )


def stop(port: int, session_id: str, mode: str = "quit", close_window: bool = False) -> Dict[str, Any]:
    return _request(
        "POST", port, f"/sessions/{session_id}/stop", json={"mode": mode, "close_window": close_window}
    )


def upload_image(
    port: int,
    session_id: str,
    filename: str,
    content: bytes,
    content_type: str,
    inline: bool = False,
) -> Dict[str, Any]:
    """Upload an image into a session. With ``inline`` the session-host
    skips pasting the path into the PTY and just returns it (issue #41)."""
    return _request(
        "POST",
        port,
        f"/sessions/{session_id}/image",
        files={"file": (filename, content, content_type)},
        params={"inline": "1"} if inline else None,
    )
