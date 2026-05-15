"""Thin HTTP client for the loopback PTY session-host.

The webapp owns all auth, Tailscale gating, and WebAuthn; it talks to the
session-host (``app/session_host/server.py``) purely over loopback. These
are blocking ``requests`` calls — webapp routes wrap them in
``asyncio.to_thread`` so the event loop never stalls. The WebSocket proxy
is handled separately in ``app/webapp/server.py``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = 8.0
# Creating a session involves a cold-start pywinpty spawn of ``claude``,
# which can easily exceed 8 s on a freshly-booted box (claude CLI + DLLs
# still in disk cache). All other calls are cheap loopback ops and stay
# on the short timeout.
_CREATE_TIMEOUT = 45.0


def base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def ws_url(port: int, session_id: str, role: str = "phone") -> str:
    return f"ws://127.0.0.1:{port}/sessions/{session_id}/ws?role={role}"


class SessionHostError(RuntimeError):
    """Raised when the session-host is unreachable or returns an error."""

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


def _request(method: str, port: int, path: str, timeout: float = _TIMEOUT, **kwargs) -> Any:
    url = base_url(port) + path
    try:
        resp = requests.request(method, url, timeout=timeout, **kwargs)
    except requests.RequestException as exc:
        raise SessionHostError(
            f"session-host unreachable on :{port} ({exc})", status=503
        ) from exc
    if resp.status_code >= 400:
        detail = _detail(resp)
        raise SessionHostError(detail, status=resp.status_code)
    try:
        return resp.json()
    except ValueError:
        return {}


def _detail(resp: requests.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("detail"):
            return str(body["detail"])
    except ValueError:
        pass
    return f"session-host HTTP {resp.status_code}"


def health(port: int) -> bool:
    try:
        resp = requests.get(base_url(port) + "/healthz", timeout=2.0)
        return resp.status_code == 200
    except requests.RequestException:
        return False


def create_session(
    port: int, project_dir: str, name: str, flags: str, kind: str = "pty"
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


def stop(port: int, session_id: str, mode: str = "quit") -> Dict[str, Any]:
    return _request(
        "POST", port, f"/sessions/{session_id}/stop", json={"mode": mode}
    )


def upload_image(
    port: int, session_id: str, filename: str, content: bytes, content_type: str
) -> Dict[str, Any]:
    return _request(
        "POST",
        port,
        f"/sessions/{session_id}/image",
        files={"file": (filename, content, content_type)},
    )
