"""Cross-router helpers — no router imports another router; shared utility
lives here instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from fastapi import Request, WebSocket

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


async def maybe_json(request: Request) -> Dict[str, Any]:
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def cert_present() -> bool:
    return (
        (PROJECT_ROOT / "webapp" / "certificates" / "cert.pem").exists()
        and (PROJECT_ROOT / "webapp" / "certificates" / "key.pem").exists()
    )


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def should_mirror_to_pc(
    show_local_window: bool, request: Request, body: Dict[str, Any]
) -> bool:
    """Whether a PTY launch should open the PC mirror window (issue #20).

    The mirror is for the **phone-launch** case (the PC has no window yet).
    It is skipped when the launch came from the PC itself — either by
    loopback IP, or (issue #159) because a desktop browser set
    ``desktop: true`` in the launch body: such a client already renders the
    streamed terminal in-page, so a separate Edge ``--app`` window would be
    redundant. The IP check alone misses a desktop reaching the app over the
    Tailscale/Cloudflare tunnel, which is non-loopback yet still the PC.
    """
    # Imported here to avoid a module-load cycle (middleware imports nothing
    # from the routers package, but keep the dependency edge one-directional).
    from app.webapp.middleware import LOOPBACK_HOSTS

    return (
        bool(show_local_window)
        and client_ip(request) not in LOOPBACK_HOSTS
        and not bool(body.get("desktop"))
    )


def client_ip_ws(websocket: WebSocket) -> str:
    return websocket.client.host if websocket.client else "?"
