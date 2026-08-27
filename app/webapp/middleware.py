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
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from src import api_tokens
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


def via_cloudflare(headers: Mapping[str, str]) -> bool:
    return any(h in headers for h in _CLOUDFLARE_HEADERS)


def is_pc_itself(client_host: str, headers: Mapping[str, str]) -> bool:
    """Is this really the PC talking to itself?

    The loopback bypass exists for the PC's own probes and mirror windows,
    and it skips *everything* — bearer token, Tailscale check, passkey. What
    keeps public-tunnel traffic out of it is that cloudflared runs on this
    host and dials the webapp over loopback, so the request only looks
    non-loopback once uvicorn's proxy-header handling rewrites the client
    address from the edge's ``X-Forwarded-For``. That rewrite is an uvicorn
    default we neither pass explicitly nor pin, and it does not apply at all
    when the webapp is bound dual-stack and cloudflared resolves ``::1``.

    The edge stamps its own headers on every request it forwards and a
    caller cannot remove them, so they are the one signal that survives
    however the client address resolves. A request carrying them is never
    the PC, whatever it claims to be.
    """
    return client_host in LOOPBACK_HOSTS and not via_cloudflare(headers)


def credential_accepted(cfg: Any, presented: str, method: str, path: str) -> bool:
    """Does ``presented`` satisfy either credential class *for this request*?

    Shared by the HTTP middleware and the WebSocket gate so the two cannot
    reach different verdicts on the same config. Callers must first decide
    that a credential is *required* (see :func:`credential_required`) —
    this answers only whether the one presented is good here.

    ``method`` / ``path`` are not optional: a minted token may be job-scoped,
    and scope is per-request. Accepting a match without re-checking scope
    would let a Stream Deck token — deliberately narrowed to one job's run
    endpoint — reach anything else that shares this check.
    """
    token = (getattr(cfg, "auth_token", "") or "").strip()
    if presented and token and hmac.compare_digest(presented, token):
        return True
    minted = getattr(cfg, "api_tokens", None) or []
    match = api_tokens.find_match(presented, minted)
    if match is None:
        return False
    return api_tokens.scope_rejection(match, method, path) is None


def credential_required(cfg: Any) -> bool:
    """True when *either* credential class is configured.

    A config holding only minted ``api_tokens`` must not be an open gate —
    the legacy ``auth_token`` being empty is not "auth is off".
    """
    token = (getattr(cfg, "auth_token", "") or "").strip()
    minted = getattr(cfg, "api_tokens", None) or []
    return bool(token or minted)


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
        lambda p: p.startswith("/api/board/chief/"),
        "passkey",
        "Fleet chief (#245): ensure spawns + types into a PTY, settings steer "
        "it — both terminal-grade. Loopback (the daily job, the /chief skill) "
        "bypasses like everything else.",
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
        lambda p: (
            p.startswith("/api/life-os/skills/") and p.endswith("/conversations")
        ),
        "passkey",
        "Life OS conversation index (#727): the digest — topic, decisions, open "
        "loops — is a distillation of terminal content, so it is gated exactly "
        "like the captures it summarises.",
    ),
    (
        lambda p: p == "/api/life-os/conversations/search",
        "passkey",
        "Life OS conversation search (#727): searches the full capture text, so "
        "a hit can quote anything the terminal ever showed.",
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
    if is_pc_itself(client_host, request.headers):
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
    """Require Authorization: Bearer <token> on API endpoints (non-loopback only).

    Two credential classes (issue #72): the legacy ``auth_token`` (full
    access, unchanged) and minted ``api_tokens`` records — full-scope
    (``"*"``) tokens behave like the legacy one; job-scoped tokens may
    only call ``POST /api/jobs/<id>/run`` on their allowed jobs. A
    matched minted token's identity is surfaced on ``request.state``
    (``token_id`` / ``token_label``) for run-record provenance.
    """

    def __init__(self, app, get_config):
        super().__init__(app)
        self._get_config = get_config

    async def dispatch(self, request: Request, call_next):
        client_host = request.client.host if request.client else ""
        is_loopback = is_pc_itself(client_host, request.headers)
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

        cfg = self._get_config()
        token = (getattr(cfg, "auth_token", "") or "").strip()
        minted = getattr(cfg, "api_tokens", None) or []
        # Auth is enforced when EITHER credential class is configured —
        # a config with only minted tokens must not be an open gate.
        if not credential_required(cfg) or is_loopback:
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

        if presented and token and hmac.compare_digest(presented, token):
            return await call_next(request)

        match = api_tokens.find_match(presented, minted)
        if match is not None:
            rejection = api_tokens.scope_rejection(match, request.method, path)
            if rejection is not None:
                return JSONResponse(status_code=403, content={"detail": rejection})
            request.state.token_id = match.id
            request.state.token_label = match.label
            # In-memory freshness stamp; persisted opportunistically on
            # the next mint/revoke save (see src.api_tokens).
            api_tokens.touch_last_used(minted, match.id)
            return await call_next(request)

        return JSONResponse(
            status_code=401,
            content={"detail": "missing or invalid bearer token"},
            headers={"WWW-Authenticate": 'Bearer realm="launcher"'},
        )
