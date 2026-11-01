"""Credential checks shared by the HTTP middleware and the WebSocket gates."""

from __future__ import annotations

from typing import Any

from fastapi import Request

from src.auth import api_tokens
from src.auth.api_tokens import constant_time_equals


def credential_accepted(cfg: Any, presented: str, method: str, path: str) -> bool:
    """Does ``presented`` satisfy either credential class *for this request*?

    The yes/no form of the gate, for callers that only need the verdict —
    today :func:`ws_terminal_gate` (shared by the ``sessions`` and
    ``desktop_screen`` WebSocket routes) and the WebSocket handshake in
    ``app.webapp.routers.jobs_run_store_routes``, which can do nothing with a
    reason but close the socket.
    :meth:`BearerTokenMiddleware.dispatch` deliberately keeps its own
    longer-form version of these same steps: it has to tell a scope
    rejection (403, with the reason in the body) apart from a bad
    credential (401), and it stamps the matched token onto the request.
    Keep the two in step — this is the accept rule they must agree on.

    Callers must first decide that a credential is *required* (see
    :func:`credential_required`) — this answers only whether the one
    presented is good here.

    ``method`` / ``path`` are not optional: a minted token may be job-scoped,
    and scope is per-request. Accepting a match without re-checking scope
    would let a Stream Deck token — deliberately narrowed to one job's run
    endpoint — reach anything else that shares this check.
    """
    token = (getattr(cfg, "auth_token", "") or "").strip()
    if presented and token and constant_time_equals(presented, token):
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


def _presented_credential(request: Request) -> str:
    """The bearer the caller sent: the ``Authorization`` header, else ``?token=``."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        presented = auth_header[7:].strip()
        if presented:
            return presented
    return request.query_params.get("token", "").strip()
