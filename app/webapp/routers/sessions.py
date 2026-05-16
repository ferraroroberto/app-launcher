"""Live PTY sessions — list, stop, upload image, WebSocket proxy.

The WS proxy is the only endpoint where auth is re-applied inline:
Starlette middleware doesn't see WebSocket handshakes, so the Tailscale
+ bearer + passkey checks live here.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import Any, Dict

from fastapi import APIRouter, File, HTTPException, Request, UploadFile, WebSocket
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect as ws_connect

from src import audit, session_client
from src.webapp_config import WebappConfig
from src.webauthn_gate import WebAuthnGate

from app.webapp.middleware import (
    LOOPBACK_HOSTS,
    client_in_tailnet,
    via_cloudflare,
)
from app.webapp.routers._helpers import client_ip, client_ip_ws, maybe_json

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/claude-code/sessions")
async def claude_sessions(request: Request) -> Dict[str, Any]:
    """List launcher-owned PTY sessions (public, token-gated)."""
    cfg: WebappConfig = request.app.state.webapp_config
    try:
        sessions = await asyncio.to_thread(
            session_client.list_sessions, cfg.session_host_port
        )
    except session_client.SessionHostError as exc:
        logger.debug(f"session list failed: {exc}")
        sessions = []
    return {"sessions": sessions}


@router.post("/api/claude-code/sessions/{sid}/stop")
async def stop_claude_session(sid: str, request: Request) -> Dict[str, Any]:
    """Stop a PTY session — quit | interrupt | kill (public, token-gated)."""
    cfg: WebappConfig = request.app.state.webapp_config
    body = await maybe_json(request)
    mode = str(body.get("mode") or "quit")
    close_window = bool(body.get("close_window", False))
    try:
        result = await asyncio.to_thread(
            session_client.stop, cfg.session_host_port, sid, mode, close_window
        )
    except session_client.SessionHostError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    audit.audit_event(
        "session_stop", session=sid, mode=mode, close_window=close_window, client=client_ip(request)
    )
    audit.session_log(sid, "stop", mode=mode, close_window=close_window)
    return result


@router.post("/api/claude-code/sessions/{sid}/image")
async def session_image(
    sid: str, request: Request, file: UploadFile = File(...)
) -> Dict[str, Any]:
    """Upload an image into a session (Tailscale-only + passkey)."""
    cfg: WebappConfig = request.app.state.webapp_config
    content = await file.read()
    try:
        result = await asyncio.to_thread(
            session_client.upload_image,
            cfg.session_host_port,
            sid,
            file.filename or "image.png",
            content,
            file.content_type or "application/octet-stream",
        )
    except session_client.SessionHostError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    audit.session_log(
        sid, "image", path=result.get("path"), bytes=len(content)
    )
    return result


@router.websocket("/api/claude-code/sessions/{sid}/ws")
async def proxy_session_ws(websocket: WebSocket, sid: str) -> None:
    """Tailscale-only + passkey-gated WebSocket proxy to the session-host.

    Browser ⇄ webapp ⇄ session-host. The webapp is the single auth
    choke point — WebSockets bypass the HTTP middleware, so the same
    Tailscale + bearer + passkey checks are re-applied here.
    """
    cfg: WebappConfig = websocket.app.state.webapp_config
    gate: WebAuthnGate = websocket.app.state.webauthn_gate
    client_host = websocket.client.host if websocket.client else ""

    # Accept first, *then* gate — so the browser receives the close
    # code + reason and can show a clear message instead of a bare
    # "Disconnected" (closing before accept just fails the handshake).
    await websocket.accept()

    if client_host not in LOOPBACK_HOSTS:
        if via_cloudflare(websocket.headers):
            await websocket.close(
                code=4403,
                reason="terminal is Tailscale-only — blocked on the public tunnel",
            )
            return
        if not client_in_tailnet(
            client_host, getattr(cfg, "tailnet_allowlist", [])
        ):
            await websocket.close(
                code=4403, reason="terminal is Tailscale-only"
            )
            return
        token = (cfg.auth_token or "").strip()
        if token:
            presented = websocket.query_params.get("token", "").strip()
            if not (presented and hmac.compare_digest(presented, token)):
                await websocket.close(
                    code=4401, reason="missing or invalid bearer token"
                )
                return
        if WebAuthnGate.configured(cfg):
            tt = websocket.query_params.get("tt", "").strip()
            if not gate.valid_terminal_token(tt):
                await websocket.close(
                    code=4401, reason="passkey unlock required"
                )
                return

    # The phone drives the PTY size; the loopback PC mirror window
    # connects as role=pc and never resizes it (see session-host).
    role = "pc" if client_host in LOOPBACK_HOSTS else "phone"
    upstream_url = session_client.ws_url(cfg.session_host_port, sid, role)
    try:
        async with ws_connect(upstream_url) as upstream:
            audit.audit_event(
                "ws_open", session=sid, client=client_ip_ws(websocket)
            )
            audit.session_log(sid, "ws_open", client=client_ip_ws(websocket))
            await _proxy_websocket(websocket, upstream, sid)
    except (OSError, WebSocketDisconnect) as exc:
        logger.debug(f"WS proxy {sid[:8]} ended: {exc}")
        try:
            await websocket.close(
                code=4502, reason="session-host unreachable"
            )
        except RuntimeError:
            pass
    finally:
        audit.session_log(sid, "ws_close")


async def _proxy_websocket(client: WebSocket, upstream, sid: str) -> None:
    """Pump frames both ways between the browser and the session-host.

    Server→client frames are raw terminal output. Client→server frames are
    JSON control messages — ``input`` frames are tee'd to the per-session
    audit log on the way through.
    """

    async def client_to_upstream() -> None:
        while True:
            raw = await client.receive_text()
            try:
                msg = json.loads(raw)
                if isinstance(msg, dict) and msg.get("type") == "input":
                    audit.session_input(sid, str(msg.get("data") or ""))
            except (ValueError, TypeError):
                pass
            await upstream.send(raw)

    async def upstream_to_client() -> None:
        async for message in upstream:
            if isinstance(message, bytes):
                message = message.decode("utf-8", errors="replace")
            await client.send_text(message)
        # session-host closed its side (session ended) — close the browser.
        await client.close(code=4000)

    c2u = asyncio.create_task(client_to_upstream())
    u2c = asyncio.create_task(upstream_to_client())
    done, pending = await asyncio.wait(
        {c2u, u2c}, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
    for task in done:
        exc = task.exception()
        if exc and not isinstance(exc, WebSocketDisconnect):
            logger.debug(f"WS proxy {sid[:8]} task ended: {exc}")
