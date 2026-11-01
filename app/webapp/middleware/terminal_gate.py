"""Tailscale-only (+ passkey) gates for terminal-grade HTTP and WebSocket routes."""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from fastapi import Request, WebSocket
from fastapi.responses import JSONResponse

from app.webapp.middleware import trust
from app.webapp.middleware.credentials import credential_accepted, credential_required
from app.webapp.middleware.routes import _terminal_guard_level
from src.auth.webauthn_gate import WebAuthnGate


def terminal_http_gate(
    request: Request, *, level: Optional[str] = None,
) -> Optional[JSONResponse]:
    """Enforce Tailscale-only (+ passkey) access on terminal HTTP endpoints.

    Returns an error response to short-circuit with, or ``None`` to allow.
    Loopback callers are handled by the middleware before this runs.
    """
    level = level or _terminal_guard_level(request.url.path)
    if level is None:
        return None
    if trust.via_cloudflare(request.headers):
        return JSONResponse(
            status_code=403,
            content={"detail": "terminal endpoints are not reachable over the public tunnel"},
        )
    cfg = request.app.state.webapp_config
    client_host = request.client.host if request.client else ""
    if not trust.client_in_tailnet(client_host, getattr(cfg, "tailnet_allowlist", [])):
        return JSONResponse(
            status_code=403,
            content={"detail": "terminal endpoints are Tailscale-only"},
        )
    if level == "passkey" and WebAuthnGate.configured(cfg):
        gate: WebAuthnGate = request.app.state.webauthn_gate
        presented = request.headers.get("x-terminal-token") or (
            request.query_params.get("tt", "")
        )
        if not gate.valid_terminal_token(presented):
            return JSONResponse(
                status_code=401,
                content={"detail": "passkey unlock required"},
            )
    return None


def ws_terminal_gate(
    websocket: WebSocket, *, label: str = "terminal",
) -> Optional[Tuple[int, str]]:
    """``(code, reason)`` to close a terminal-grade WebSocket with, or
    ``None`` when it may stream.

    The one gate for every WebSocket this strict — today
    ``sessions.proxy_session_ws`` and ``desktop_screen.screen_ws`` — in the
    same order as :func:`terminal_http_gate`'s HTTP siblings: the PC itself
    (loopback with no edge header) bypasses; anything else must not have
    come over the public edge, must be on the tailnet, must present a
    credential when one is configured, and must hold a passkey terminal
    token when the passkey gate is configured. WebSocket handshakes
    re-apply this inline because Starlette middleware never sees them.

    ``label`` names the service in the closed-socket reason text
    (``"terminal"`` / ``"screen"``); the legs and close codes are shared.
    """
    cfg: Any = websocket.app.state.webapp_config
    client_host = websocket.client.host if websocket.client else ""
    if trust.is_pc_itself(client_host, websocket.headers):
        return None
    if trust.via_cloudflare(websocket.headers):
        return 4403, f"{label} is Tailscale-only — blocked on the public tunnel"
    if not trust.client_in_tailnet(client_host, getattr(cfg, "tailnet_allowlist", [])):
        return 4403, f"{label} is Tailscale-only"
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


def terminal_reachability(request: Request) -> Dict[str, Any]:
    """Can the *current* connection reach the live terminal at all?

    The terminal is Tailscale-only by design — so the SPA can ask up front
    and explain it, rather than letting the user open a terminal that will
    only ever say "Disconnected". Used by ``/api/status``.
    """
    client_host = request.client.host if request.client else ""
    if trust.is_pc_itself(client_host, request.headers):
        return {"reachable": True, "reason": "loopback"}
    if trust.via_cloudflare(request.headers):
        return {
            "reachable": False,
            "reason": (
                "The live terminal is Tailscale-only — it is blocked on the "
                "public Cloudflare tunnel by design. Open the launcher over "
                "your Tailscale URL (https://<pc>.<tailnet>.ts.net:8445) to "
                "use it."
            ),
        }
    cfg = request.app.state.webapp_config
    if not trust.client_in_tailnet(client_host, getattr(cfg, "tailnet_allowlist", [])):
        return {
            "reachable": False,
            "reason": (
                f"This connection ({client_host}) is not on your tailnet. "
                "Open the launcher over your Tailscale URL, or add this "
                "network to tailnet_allowlist in config/webapp_config.json."
            ),
        }
    return {"reachable": True, "reason": "tailnet"}
