"""``BearerTokenMiddleware``: the single auth choke point for the HTTP surface."""

from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.webapp.middleware import trust
from app.webapp.middleware.credentials import _presented_credential, credential_required
from app.webapp.middleware.routes import (
    AUTH_EXEMPT_EXACT,
    AUTH_EXEMPT_PREFIXES,
    _is_webhook_hook_path,
)
from app.webapp.middleware.terminal_gate import terminal_http_gate
from src.auth import api_tokens
from src.auth import delegate_token
from src.auth.api_tokens import constant_time_equals

# The logger behind webapp/auth.log, the auth trail that persists on the live
# tray, so every delegate-token request is reviewable after the fact.
auth_logger = logging.getLogger("launcher.auth")


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
        is_loopback = trust.is_pc_itself(client_host, request.headers)
        path = request.url.path
        cfg = self._get_config()

        # The delegate token is decided before every other leg, the terminal
        # gate included, so a delegate caller on the tailnet is still held to
        # its allow-list. Loopback never reaches it and keeps its bypass.
        if not is_loopback:
            presented = _presented_credential(request)
            if delegate_token.matches(presented, getattr(cfg, "delegate_token", None)):
                return await _delegate_dispatch(request, call_next, client_host)

        # Terminal endpoints are Tailscale-only (+ passkey for the
        # interactive ones). Enforced even when no bearer token is
        # configured. The PC itself (loopback) is trusted and skips it.
        if not is_loopback:
            gate_err = terminal_http_gate(request)
            if gate_err is not None:
                return gate_err

        if _is_webhook_hook_path(path):
            return await call_next(request)

        token = (getattr(cfg, "auth_token", "") or "").strip()
        minted = getattr(cfg, "api_tokens", None) or []
        # Auth is enforced when EITHER credential class is configured —
        # a config with only minted tokens must not be an open gate.
        # The accept rule below is the reporting form of
        # `credential_accepted`, which the WS gate uses; changing one means
        # changing both.
        if not credential_required(cfg) or is_loopback:
            return await call_next(request)

        if path in AUTH_EXEMPT_EXACT or any(
            path.startswith(p) for p in AUTH_EXEMPT_PREFIXES
        ):
            return await call_next(request)

        presented = _presented_credential(request)
        if presented and token and constant_time_equals(presented, token):
            return await call_next(request)

        match = api_tokens.find_match(presented, minted)
        if match is not None:
            rejection = api_tokens.scope_rejection(match, request.method, path)
            if rejection is not None:
                return JSONResponse(status_code=403, content={"detail": rejection})
            request.state.token_id = match.id
            request.state.token_label = match.label
            # In-memory freshness stamp; persisted opportunistically on
            # the next mint/revoke save (see src.auth.api_tokens).
            api_tokens.touch_last_used(minted, match.id)
            return await call_next(request)

        return JSONResponse(
            status_code=401,
            content={"detail": "missing or invalid bearer token"},
            headers={"WWW-Authenticate": 'Bearer realm="launcher"'},
        )


async def _delegate_dispatch(request: Request, call_next, client_host: str):
    """Serve a delegate-token request on its allow-list, refuse anything else.

    The refusal is a 403 with its own reason, so a caller can tell "this token
    may not do that" apart from a bad credential's 401. Every request is
    logged with its method, path and outcome, never the token.
    """
    method, path = request.method, request.url.path
    if not delegate_token.allowed(method, path):
        auth_logger.info(f"🔒 delegate token from {client_host}: {method} {path} → 403 refused")
        return JSONResponse(status_code=403, content={"detail": delegate_token.REFUSAL})
    request.state.delegate = True
    response = await call_next(request)
    auth_logger.info(
        f"ℹ️ delegate token from {client_host}: {method} {path} → {response.status_code}"
    )
    return response
