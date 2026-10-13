"""Screen view (#1520): the PC's desktop, live, on the phone.

``GET /api/screen/monitors`` lists the displays; ``WS /api/screen/ws`` streams
one of them as JPEG binary frames. Capture lives in :mod:`src.desktop_capture`
and runs only inside a connected socket.

A picture of the desktop shows whatever is on it, so this is terminal-grade
content (#1036): the HTTP route carries a ``passkey`` row in
``middleware._TERMINAL_GUARD_RULES``, and the WebSocket, which the HTTP
middleware never sees, re-applies the same legs inline before a single grab:
refused over the Cloudflare edge, Tailscale-only, bearer token, passkey. The
order and close codes are ``sessions.proxy_session_ws``'s, so the client
reads them the same way.

Protocol: the server sends binary JPEG frames and JSON text status messages
(``{"type": "status", "state": "unavailable" | "live"}``); the client sends
the text ``ack`` after drawing each frame, and the next frame waits for it.

Input (#1521): every other text frame the client sends is a JSON input
message (click, down/move/up, scroll, text, key), handed to a
:class:`src.desktop_input.InputSession` for this socket's monitor. The reader
that receives them only starts after the gate above has passed, so input
rides the same socket and the same gate as the picture; there is no other
input path (``tests/test_desktop_input.py`` fails if one appears). When the
socket goes away, every key and button the session still holds is released.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

from src import audit, desktop_capture, desktop_input
from src.webapp_config import WebappConfig
from src.webauthn_gate import WebAuthnGate

from app.webapp.middleware import (
    LOOPBACK_HOSTS,
    client_in_tailnet,
    credential_accepted,
    credential_required,
    via_cloudflare,
)
from app.webapp.routers._helpers import audit_off_loop, client_ip_ws

logger = logging.getLogger(__name__)

router = APIRouter()

WS_PATH = "/api/screen/ws"


@router.get("/api/screen/monitors")
async def screen_monitors() -> Dict[str, Any]:
    """The displays the Screen view can show; no capture happens here."""
    monitors = await asyncio.to_thread(desktop_capture.list_monitors)
    return {"monitors": [m.to_dict() for m in monitors]}


async def _gate_refusal(websocket: WebSocket) -> Optional[Tuple[int, str]]:
    """``(code, reason)`` to close with, or ``None`` when the socket may stream.

    The same legs, in the same order, as ``sessions.proxy_session_ws``: the
    PC itself (loopback with no edge header) bypasses; anything else must not
    have come over the public edge, must be on the tailnet, must present a
    credential when one is configured, and must hold a passkey terminal token
    when the passkey gate is configured.
    """
    cfg: WebappConfig = websocket.app.state.webapp_config
    client_host = websocket.client.host if websocket.client else ""
    if client_host in LOOPBACK_HOSTS and not via_cloudflare(websocket.headers):
        return None
    if via_cloudflare(websocket.headers):
        return 4403, "screen is Tailscale-only, blocked on the public tunnel"
    if not client_in_tailnet(client_host, getattr(cfg, "tailnet_allowlist", [])):
        return 4403, "screen is Tailscale-only"
    if credential_required(cfg):
        presented = websocket.query_params.get("token", "").strip()
        if not credential_accepted(cfg, presented, "GET", websocket.url.path):
            return 4401, "missing or invalid bearer token"
    if WebAuthnGate.configured(cfg):
        gate: WebAuthnGate = websocket.app.state.webauthn_gate
        tt = websocket.query_params.get("tt", "").strip()
        if not gate.valid_terminal_token(tt):
            return 4401, "passkey unlock required"
    return None


def _int_param(websocket: WebSocket, name: str) -> Optional[int]:
    raw = websocket.query_params.get(name, "").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


async def _read_client(
    websocket: WebSocket,
    acked: asyncio.Event,
    stopped: asyncio.Event,
    session: Optional[desktop_input.InputSession] = None,
) -> None:
    """Turn the client's ``ack`` messages into ``acked`` and hand every other
    text frame to ``session`` as input, in order; a disconnect stops.

    Input runs off the loop: ``SendInput`` is quick, but a slow low-level
    hook elsewhere on the PC can hold it, and that must not stall the app.
    """
    try:
        while True:
            text = await websocket.receive_text()
            if text == "ack":
                acked.set()
            elif session is not None:
                await asyncio.to_thread(session.handle_text, text)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        stopped.set()


@router.websocket(WS_PATH)
async def screen_ws(websocket: WebSocket) -> None:
    """Stream one monitor until the viewer goes away."""
    # Accept first, then gate, so the browser gets the close code and reason.
    await websocket.accept()
    refusal = await _gate_refusal(websocket)
    if refusal is not None:
        await websocket.close(code=refusal[0], reason=refusal[1])
        return

    monitors = await asyncio.to_thread(desktop_capture.list_monitors)
    monitor = desktop_capture.pick_monitor(monitors, _int_param(websocket, "monitor"))
    if monitor is None:
        await websocket.close(code=4404, reason="no such monitor")
        return
    width = min(
        desktop_capture.clamp_width(_int_param(websocket, "width")), monitor.width
    )

    client = client_ip_ws(websocket)
    started = time.monotonic()
    await audit_off_loop(
        audit.audit_event, "screen_open", client=client, monitor=monitor.id,
        rect=list(monitor.rect), width=width,
    )

    acked = asyncio.Event()
    acked.set()
    stopped = asyncio.Event()

    # Counted here rather than taken from stream_frames' return value, so a
    # send that fails mid-stream still audits the frames that did go out.
    sent = {"frames": 0}

    async def send_frame(data: bytes) -> None:
        await websocket.send_bytes(data)
        sent["frames"] += 1

    async def send_status(state: str) -> None:
        await websocket.send_text(json.dumps({"type": "status", "state": state}))

    session = desktop_input.InputSession(desktop_input.get_injector(), monitor, monitors)
    reader = asyncio.create_task(_read_client(websocket, acked, stopped, session))
    try:
        await desktop_capture.stream_frames(
            lambda: desktop_capture.grab_monitor(monitor),
            send_frame, send_status, acked, stopped, width=width,
        )
    except (WebSocketDisconnect, RuntimeError, OSError) as exc:
        logger.debug("Screen socket ended while sending: %s", exc)
    finally:
        stopped.set()
        # First, and synchronously: nothing below may stop a dropped phone
        # from lifting a held Ctrl or mouse button on the PC.
        try:
            session.release_all()
        except Exception as exc:  # noqa: BLE001 - the close audit must still run
            logger.warning("⚠️ Screen: releasing held input failed: %s", exc)
        reader.cancel()
        try:
            await reader
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await audit_off_loop(
            audit.audit_event, "screen_close", client=client, monitor=monitor.id,
            duration_s=round(time.monotonic() - started, 1), frames=sent["frames"],
            inputs=session.count,
        )
