"""Live PTY sessions — list, stop, upload image, WebSocket proxy.

The WS proxy is the only endpoint where auth is re-applied inline:
Starlette middleware doesn't see WebSocket handshakes, so the Tailscale
+ bearer + passkey checks live here.

Split off a single-file god-router (``/codebase-audit``): the
voice-dictation, screenshot-OCR, and hub read-aloud proxy endpoints live
in :mod:`app.webapp.routers.media_proxy` (mounted here via
``include_router`` so ``app/webapp/server.py`` still registers one
``sessions.router``) — the same pattern ``app.webapp.routers.jobs`` uses
for its own webhook/run-store split.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, File, HTTPException, Request, UploadFile, WebSocket
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import InvalidHandshake

from src import audit, board, launcher, session_client
from src.webapp_config import SESSION_HOST_PORT_ENV, WebappConfig
from src.webauthn_gate import WebAuthnGate

from app.webapp.middleware import (
    LOOPBACK_HOSTS,
    client_in_tailnet,
    via_cloudflare,
)
from app.webapp.routers import media_proxy
from app.webapp.routers._helpers import (
    client_ip,
    client_ip_ws,
    maybe_json,
    mirror_url,
)

logger = logging.getLogger(__name__)
router = APIRouter()
router.include_router(media_proxy.router)


@router.get("/api/claude-code/sessions")
async def claude_sessions(request: Request) -> Dict[str, Any]:
    """List launcher-owned PTY sessions (public, token-gated).

    Each session is joined with the shared cross-tab title (fleet-config#302,
    via app-launcher#396) as ``shared_name``/``shared_name_source`` — the same
    agent-aware claim walk the Board tab's ``merge_sessions()`` uses
    (``board.attach_shared_names``), so a live session resolves to the same
    state row — and therefore shows the same title — on both tabs.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    try:
        sessions = await asyncio.to_thread(
            session_client.list_sessions, cfg.session_host_port
        )
    except session_client.SessionHostError as exc:
        logger.debug(f"session list failed: {exc}")
        sessions = []
    if sessions:
        state = await asyncio.to_thread(
            board.read_sessions_state, Path(cfg.sessions_state_file)
        )
        sessions = board.attach_shared_names(sessions, state["rows"])
    return {"sessions": sessions}


@router.post("/api/claude-code/sessions/{sid}/stop")
async def stop_claude_session(sid: str, request: Request) -> Dict[str, Any]:
    """Stop a PTY session — graceful /quit then force-fallback (public, token-gated)."""
    cfg: WebappConfig = request.app.state.webapp_config
    body = await maybe_json(request)
    mode = str(body.get("mode") or "quit")
    # Every stop now closes the window (issue #253 unified the button), so
    # always Win32-close the PC mirror window (issue #20). close_mirror_window
    # first tries the HWND stashed at spawn time, then falls back to a fresh
    # title-scan of live windows (issue #199) so it works even after a webapp
    # restart wiped the in-memory registry. Best-effort: swallow any exception
    # so a busted HWND can't keep the session alive. The cooperative WS
    # shutdown the session-host fires is a further fallback for when no
    # matching window is on the desktop.
    try:
        posted = launcher.close_mirror_window(sid)
        logger.debug(
            f"close_mirror_window({sid[:8]}) returned {posted}; "
            f"forwarding stop({mode!r}) to session-host"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"🛑 mirror window close raised for {sid[:8]}: {exc}")
    try:
        result = await asyncio.to_thread(
            session_client.stop, cfg.session_host_port, sid, mode
        )
    except session_client.SessionHostError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    audit.audit_event(
        "session_stop", session=sid, mode=mode, client=client_ip(request)
    )
    audit.session_log(sid, "stop", mode=mode)
    return result


@router.post("/api/claude-code/sessions/{sid}/rename")
async def rename_claude_session(sid: str, request: Request) -> Dict[str, Any]:
    """Set or clear a manual title override for a session (issue #458).

    Wins over every auto-derived title (``live_title``/``prompt_title``/
    ``shared_name``) in the client's precedence (``sessions.js::sessionTitle``)
    — the one rename path that works identically across every launcher-
    supported agent, including detached (``kind=remote``) sessions, since it
    needs no agent-native OSC title support. An empty ``title`` clears the
    override, reverting to the automatic precedence.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    body = await maybe_json(request)
    title = str(body.get("title") or "").strip()
    try:
        result = await asyncio.to_thread(
            session_client.rename, cfg.session_host_port, sid, title
        )
    except session_client.SessionHostError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    audit.audit_event("session_rename", session=sid, client=client_ip(request))
    return result


@router.post("/api/claude-code/sessions/{sid}/mirror")
async def mirror_claude_session(sid: str, request: Request) -> Dict[str, Any]:
    """Open (or focus) the PC mirror window for an existing session (issue #282).

    Desktop parity with the launch flow: clicking a session row on a desktop
    browser should get the same dedicated Edge ``--app`` window a *new*-session
    launch opens (``should_mirror_to_pc``), not render the terminal inside the
    user's own browser — so closing it can't tear down the controlling window.
    A second click focuses the live window rather than spawning a duplicate
    (the HWND registry is keyed by sid; a duplicate would orphan the first).
    Stop still closes it — the open path registers the HWND exactly as the
    launch path does (issue #20).

    The phone never calls this (it streams in-page). When local-window
    mirroring is disabled (``claude_show_local_window`` false) the launch flow
    opens no window either, so this returns ``mirrored: false`` and the caller
    falls back to the in-page terminal — preserving the old behaviour for that
    config.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    if not cfg.claude_show_local_window:
        return {"mirrored": False, "reason": "local window mirroring disabled"}
    # A disposable e2e / verify-before-ship instance (identified by the
    # session-host port override, set only by autoboot) must never spawn a real
    # Edge window or focus the canonical instance's desktop windows — the same
    # rule the orphan-mirror sweep follows (issue #278). Report ``mirrored`` so
    # the desktop client still skips the in-page terminal, but touch no window.
    if os.environ.get(SESSION_HOST_PORT_ENV, "").strip():
        return {"mirrored": True, "action": "skipped"}
    pc_url = mirror_url(request, cfg, sid)
    # Runs the win32 focus/spawn off the event loop; the spawn returns as soon
    # as Edge is launched (its HWND poll is a daemon thread), so this is quick.
    action = await asyncio.to_thread(
        launcher.open_or_focus_mirror_window, pc_url, sid
    )
    audit.audit_event(
        "session_mirror", session=sid, action=action, client=client_ip(request)
    )
    return {"mirrored": True, "action": action}


@router.post("/api/claude-code/sessions/{sid}/image")
async def session_image(
    sid: str, request: Request, file: UploadFile = File(...)
) -> Dict[str, Any]:
    """Upload an image into a session (Tailscale-only + passkey).

    ``?inline=1`` (compose bar open) tells the session-host to skip the
    paste-into-PTY step and just return the stored path so the browser
    can drop it into the compose textarea for review (issue #41).
    """
    cfg: WebappConfig = request.app.state.webapp_config
    inline = request.query_params.get("inline") in ("1", "true")
    content = await file.read()
    try:
        result = await asyncio.to_thread(
            session_client.upload_image,
            cfg.session_host_port,
            sid,
            file.filename or "image.png",
            content,
            file.content_type or "application/octet-stream",
            inline,
        )
    except session_client.SessionHostError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    audit.session_log(
        sid, "image", path=result.get("path"), bytes=len(content), inline=inline
    )
    return result


@router.post("/api/claude-code/sessions/{sid}/input")
async def session_input(sid: str, request: Request) -> Dict[str, Any]:
    """Write composed text into a session's PTY (Tailscale-only + passkey, #301).

    The Board drawer's reply path. Body: ``{"data": str, "submit": bool}``.
    Multi-line data is wrapped in bracketed-paste markers so the TUI buffers
    it as one atomic paste (#64); the submitting CR is delivered as its own
    *second* PTY write, never concatenated onto the text — the same two-frame
    ordering the compose bar's ➤ Send uses over the WS (#166), which is what
    keeps the paste-end marker from swallowing the CR.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    body = await maybe_json(request)
    data = body.get("data")
    if not isinstance(data, str) or not data.strip():
        raise HTTPException(status_code=400, detail="data must be a non-empty string")
    submit = bool(body.get("submit", True))
    payload = "\x1b[200~" + data + "\x1b[201~" if "\n" in data else data
    try:
        await asyncio.to_thread(
            session_client.send_input, cfg.session_host_port, sid, payload
        )
        if submit:
            await asyncio.to_thread(
                session_client.send_input, cfg.session_host_port, sid, "\r"
            )
    except session_client.SessionHostError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    audit.session_log(sid, "input", bytes=len(data), submit=submit)
    return {"ok": True, "bytes": len(data), "submit": submit}


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
    except (OSError, WebSocketDisconnect, InvalidHandshake) as exc:
        # InvalidHandshake covers an upstream WS upgrade rejected at the
        # HTTP layer — e.g. the session-host answering 403 for a reaped
        # or unknown session (InvalidStatus). Same "upstream not usable"
        # condition as OSError; map it to the clean 4502 close instead of
        # letting it escape as an unhandled ASGI traceback (issue #61).
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
