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
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from src.webauthn_gate import WebAuthnGate

logger = logging.getLogger(__name__)

# Loopback addresses bypass the bearer-token gate so local probes keep
# working without carrying the token. Tunnel traffic arrives with a
# non-loopback client IP and must present the token.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

AUTH_EXEMPT_PREFIXES = ("/static/", "/healthz")
AUTH_EXEMPT_EXACT = frozenset({"/", "/healthz", "/api/login"})


def _is_webhook_hook_path(path: str) -> bool:
    """``POST /api/jobs/<id>/hook`` (issue #73) authenticates itself via the
    job's provider-specific signature, never the bearer token — an external
    service (GitHub, Stripe, …) can't carry it, and the whole point is a URL
    that's safe to hand to a third party in plaintext.
    """
    return path.startswith("/api/jobs/") and path.endswith("/hook")

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


# Gated-endpoint inventory driving _terminal_guard_level, as a table of
# ``(predicate, level, comment)`` instead of an accreting if-chain (one check
# per feature/issue number) — a new gated endpoint is one row here, and the
# full inventory stays scannable in one place instead of spread across prose
# comments. Order matters only in that the first matching predicate wins;
# in practice every predicate here targets a disjoint path shape.
_TerminalGuardRule = Tuple[Callable[[str], bool], str, str]

_TERMINAL_GUARD_RULES: Tuple[_TerminalGuardRule, ...] = (
    (
        lambda p: p.startswith("/api/webauthn/"),
        "tailnet",
        "WebAuthn ceremony endpoints — Tailscale-only; no passkey check (that's what they issue).",
    ),
    (
        lambda p: p.startswith("/api/claude-code/sessions/") and p.endswith("/image"),
        "passkey",
        "Coding-tab image paste into a live PTY.",
    ),
    (
        lambda p: p.startswith("/api/claude-code/sessions/") and p.endswith("/input"),
        "passkey",
        "Board drill-down (#301): reply proxy writes into a live PTY.",
    ),
    (
        lambda p: p.startswith("/api/board/sessions/") and p.endswith("/exchange"),
        "passkey",
        "Board drill-down (#301): last-exchange surfaces transcript text (terminal-grade content).",
    ),
    (
        lambda p: p == "/api/board/issues/start",
        "passkey",
        "Board drill-down (#301): issue-start spawns a coding session.",
    ),
    (
        lambda p: p == "/api/board/dispatch",
        "passkey",
        "Board dispatch (#302): spawn + type — both terminal-grade.",
    ),
    (
        lambda p: p == "/api/transcribe" or p.startswith("/api/transcribe/"),
        "passkey",
        "Voice dictation (#165/#168): the recording is as sensitive as terminal "
        "input — covers the single-shot proxy and the streamed/SSE session proxy.",
    ),
    (
        lambda p: p == "/api/ocr",
        "passkey",
        "Screenshot OCR (#171): extracted text drops straight into the terminal compose bar.",
    ),
    (
        lambda p: p in ("/api/tts/speak", "/api/tts/summarize"),
        "passkey",
        "Read-aloud hub TTS (#203/#206) + summarize-&-read (#210): both POST the "
        "agent's last reply — terminal content. /api/tts/health stays token-only "
        "so the SPA can decide button visibility even off-tailnet.",
    ),
    (
        lambda p: p == "/api/life-os/file" or p.startswith("/api/life-os/file/"),
        "passkey",
        "Life OS private-content browser (#102): file read/delete/rename surfaces "
        "gitignored private knowledge. Skills list/launch stay public (token-gated).",
    ),
    (
        lambda p: p.startswith("/api/life-os/skills/") and p.endswith("/files"),
        "passkey",
        "Life OS per-skill file tree (#102) — same sensitivity as the file endpoint above.",
    ),
    (
        lambda p: p == "/api/system-map/image",
        "tailnet",
        "Fleet system map (#173): rendered PNG can carry fleet topology. No passkey "
        "— /api/system-map/status stays token-only so the SPA can decide the "
        "section's visibility off-tailnet, like /api/tts/health.",
    ),
)


def _terminal_guard_level(path: str) -> Optional[str]:
    """Classify a request path's terminal-gating requirement.

    ``"passkey"`` — Tailscale-only **and** a valid passkey terminal token.
    ``"tailnet"`` — Tailscale-only (the WebAuthn ceremony endpoints).
    ``None``      — not a terminal endpoint; normal bearer-token rules apply.

    Driven by :data:`_TERMINAL_GUARD_RULES` (issue #408).
    """
    for predicate, level, _comment in _TERMINAL_GUARD_RULES:
        if predicate(path):
            return level
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

        if _is_webhook_hook_path(path):
            return await call_next(request)

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
