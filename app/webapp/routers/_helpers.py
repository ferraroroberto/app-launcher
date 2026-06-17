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

    Both the phone and a desktop browser get a dedicated Edge ``--app``
    window on the PC (issue #241):

    * **Phone** — non-loopback and no ``desktop`` flag: the PC has no window
      of its own, so mirror to one.
    * **Desktop browser** — ``desktop: true`` in the launch body: mirror to a
      dedicated, independently-closable Edge window rather than rendering the
      terminal inside the user's own browser. This reverses issue #159's
      desktop-skips-mirror optimization for the PTY case — the "redundant"
      in-page render was the very thing that let Stop & Close tear down the
      controlling Chrome window, so the dedicated window is the fix, not the
      redundancy. The flag (set client-side by ``isDesktopClient``) is what
      distinguishes a desktop from a phone regardless of loopback vs tunnel.

    A non-``desktop`` loopback launch (the rare PC client that reports a
    coarse pointer) still skips the mirror and renders in-page — harmless now
    that an in-page loopback terminal is no longer mis-treated as a mirror.
    """
    # Imported here to avoid a module-load cycle (middleware imports nothing
    # from the routers package, but keep the dependency edge one-directional).
    from app.webapp.middleware import LOOPBACK_HOSTS

    if not show_local_window:
        return False
    return bool(body.get("desktop")) or client_ip(request) not in LOOPBACK_HOSTS


def client_ip_ws(websocket: WebSocket) -> str:
    return websocket.client.host if websocket.client else "?"
