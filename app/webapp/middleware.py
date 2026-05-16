"""Auth middleware + terminal-gate helpers for the launcher webapp.

The bearer-token middleware is the single auth choke point for the HTTP
surface. Loopback callers (PC itself) bypass the token; non-loopback
callers must present it, and terminal endpoints additionally require
Tailscale (+ a passkey terminal token for interactive ones).

WebSocket auth is re-applied inline in the session router because Starlette
middleware doesn't see WebSocket handshakes.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
from typing import Any, Dict, List, Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from src.webauthn_gate import WebAuthnGate

logger = logging.getLogger(__name__)

# Loopback addresses bypass the bearer-token gate so local probes keep
# working without carrying the token. Tunnel traffic arrives with a
# non-loopback client IP and must present the token.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

AUTH_EXEMPT_PREFIXES = ("/static/", "/healthz", "/install-ca")
AUTH_EXEMPT_EXACT = frozenset({"/", "/healthz", "/install-ca", "/api/login"})

# Tailscale hands every node an address in the CGNAT range. The
# interactive terminal is gated to this range (plus loopback and an
# optional user allowlist) and is refused outright over the public tunnel.
_TAILNET_CGNAT = ipaddress.ip_network("100.64.0.0/10")
# Cloudflare's tunnel adds these headers — their presence means the
# request came in over the public edge, never acceptable for a terminal.
_CLOUDFLARE_HEADERS = ("cf-ray", "cf-connecting-ip")


def via_cloudflare(headers) -> bool:
    return any(h in headers for h in _CLOUDFLARE_HEADERS)


def client_in_tailnet(client_host: str, allowlist: List[str]) -> bool:
    """True when the client IP is loopback, in the tailnet, or allowlisted."""
    try:
        ip = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    if ip.is_loopback or ip in _TAILNET_CGNAT:
        return True
    for entry in allowlist or []:
        try:
            if ip in ipaddress.ip_network(str(entry), strict=False):
                return True
        except ValueError:
            if client_host == str(entry):
                return True
    return False


def _terminal_guard_level(path: str) -> Optional[str]:
    """Classify a request path's terminal-gating requirement.

    ``"passkey"`` — Tailscale-only **and** a valid passkey terminal token.
    ``"tailnet"`` — Tailscale-only (the WebAuthn ceremony endpoints).
    ``None``      — not a terminal endpoint; normal bearer-token rules apply.
    """
    if path.startswith("/api/webauthn/"):
        return "tailnet"
    if path.startswith("/api/claude-code/sessions/") and path.endswith("/image"):
        return "passkey"
    return None


def terminal_http_gate(request: Request) -> Optional[JSONResponse]:
    """Enforce Tailscale-only (+ passkey) access on terminal HTTP endpoints.

    Returns an error response to short-circuit with, or ``None`` to allow.
    Loopback callers are handled by the middleware before this runs.
    """
    level = _terminal_guard_level(request.url.path)
    if level is None:
        return None
    if via_cloudflare(request.headers):
        return JSONResponse(
            status_code=403,
            content={"detail": "terminal endpoints are not reachable over the public tunnel"},
        )
    cfg = request.app.state.webapp_config
    client_host = request.client.host if request.client else ""
    if not client_in_tailnet(client_host, getattr(cfg, "tailnet_allowlist", [])):
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


def terminal_reachability(request: Request) -> Dict[str, Any]:
    """Can the *current* connection reach the live terminal at all?

    The terminal is Tailscale-only by design — so the SPA can ask up front
    and explain it, rather than letting the user open a terminal that will
    only ever say "Disconnected". Used by ``/api/status``.
    """
    client_host = request.client.host if request.client else ""
    if client_host in LOOPBACK_HOSTS:
        return {"reachable": True, "reason": "loopback"}
    if via_cloudflare(request.headers):
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
    if not client_in_tailnet(client_host, getattr(cfg, "tailnet_allowlist", [])):
        return {
            "reachable": False,
            "reason": (
                f"This connection ({client_host}) is not on your tailnet. "
                "Open the launcher over your Tailscale URL, or add this "
                "network to tailnet_allowlist in config/webapp_config.json."
            ),
        }
    return {"reachable": True, "reason": "tailnet"}


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Require Authorization: Bearer <token> on API endpoints (non-loopback only)."""

    def __init__(self, app, get_token):
        super().__init__(app)
        self._get_token = get_token

    async def dispatch(self, request: Request, call_next):
        client_host = request.client.host if request.client else ""
        is_loopback = client_host in LOOPBACK_HOSTS
        path = request.url.path

        # Terminal endpoints are Tailscale-only (+ passkey for the
        # interactive ones). Enforced even when no bearer token is
        # configured. The PC itself (loopback) is trusted and skips it.
        if not is_loopback:
            gate_err = terminal_http_gate(request)
            if gate_err is not None:
                return gate_err

        token = (self._get_token() or "").strip()
        if not token or is_loopback:
            return await call_next(request)

        if path in AUTH_EXEMPT_EXACT or any(
            path.startswith(p) for p in AUTH_EXEMPT_PREFIXES
        ):
            return await call_next(request)

        presented = ""
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            presented = auth_header[7:].strip()
        if not presented:
            presented = request.query_params.get("token", "").strip()

        if presented and hmac.compare_digest(presented, token):
            return await call_next(request)

        return JSONResponse(
            status_code=401,
            content={"detail": "missing or invalid bearer token"},
            headers={"WWW-Authenticate": 'Bearer realm="launcher"'},
        )
