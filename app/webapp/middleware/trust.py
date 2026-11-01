"""Who is the PC itself, and who is on the tailnet: the auth trust boundary.

Loopback trust skips every other leg (bearer token, Tailscale, passkey), so
this module is the one place that decides it. A request the public edge
forwarded is never the PC, whatever address it arrives from.

Callers that test or patch these names reach them as ``trust.<name>`` so a
patch on this module is seen by every gate.
"""

from __future__ import annotations

import ipaddress
from typing import List, Mapping

# Loopback addresses bypass the bearer-token gate so local probes keep
# working without carrying the token. Tunnel traffic arrives with a
# non-loopback client IP and must present the token.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

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
    non-loopback once the proxy-header middleware ``app.webapp.serve`` wraps
    the app in rewrites the client address from the edge's
    ``X-Forwarded-For``. That rewrite trusts ``127.0.0.1`` only, so it does
    not apply at all when the webapp is bound dual-stack and cloudflared
    resolves ``::1``.

    The edge stamps its own headers on every request it forwards and a
    caller cannot remove them, so they are the one signal that survives
    however the client address resolves. A request carrying them is never
    the PC, whatever it claims to be.
    """
    return client_host in LOOPBACK_HOSTS and not via_cloudflare(headers)


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
