"""Auth middleware + terminal-gate helpers for the launcher webapp.

The bearer-token middleware is the single auth choke point for the HTTP
surface. Loopback callers (PC itself) bypass the token; non-loopback
callers must present it, and terminal endpoints additionally require
Tailscale (+ a passkey terminal token for interactive ones).

WebSocket auth is re-applied by each terminal-grade router (``sessions``'s
proxy, ``desktop_screen``'s stream — both via :func:`ws_terminal_gate` below)
because Starlette middleware doesn't see WebSocket handshakes.

One module per concern: ``trust`` (loopback and tailnet), ``credentials``,
``routes`` (exempt paths and the terminal-guard table), ``terminal_gate`` and
``bearer`` (the middleware class). This package re-exports their public
names so importers keep ``from app.webapp.middleware import ...``; a test
that patches a trust name patches it on ``middleware.trust``.
"""

from app.webapp.middleware import trust
from app.webapp.middleware.bearer import BearerTokenMiddleware
from app.webapp.middleware.credentials import credential_accepted, credential_required
from app.webapp.middleware.routes import (
    AUTH_EXEMPT_EXACT,
    AUTH_EXEMPT_PREFIXES,
    is_webhook_hook_path,
    terminal_guard_level,
)
from app.webapp.middleware.terminal_gate import (
    terminal_http_gate,
    terminal_reachability,
    ws_terminal_gate,
)
from app.webapp.middleware.trust import (
    LOOPBACK_HOSTS,
    client_in_tailnet,
    is_pc_itself,
    via_cloudflare,
)

# Tests and docstrings across the repo name the guard as
# ``middleware._terminal_guard_level``; the alias keeps that import working.
_terminal_guard_level = terminal_guard_level

__all__ = [
    "AUTH_EXEMPT_EXACT",
    "AUTH_EXEMPT_PREFIXES",
    "BearerTokenMiddleware",
    "LOOPBACK_HOSTS",
    "_terminal_guard_level",
    "client_in_tailnet",
    "credential_accepted",
    "credential_required",
    "is_pc_itself",
    "is_webhook_hook_path",
    "terminal_guard_level",
    "terminal_http_gate",
    "terminal_reachability",
    "trust",
    "via_cloudflare",
    "ws_terminal_gate",
]
