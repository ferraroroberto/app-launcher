"""The local-llm-hub base URL the owner types in Settings: its shape and a live check.

The shape is narrow on purpose: this app calls the URL from the PC itself, so
it must name loopback or a machine on the home network (never a public host),
over http or https, with nothing after the host and port. The check asks the
hub's ``/healthz`` and says why when it cannot be reached, so a typo is refused
at the field instead of leaving the health dot ``unknown`` with no explanation.
"""

from __future__ import annotations

import ipaddress
import logging
from urllib.parse import urlsplit

from src.clients import _loopback_http

logger = logging.getLogger(__name__)

LOCAL_HUB_URL = "http://127.0.0.1:8000"
HUB_SERVICE = "local-llm-hub"
_TIMEOUT = 3.0


class HubUrlError(_loopback_http.LoopbackError):
    """The hub did not answer ``/healthz`` as a hub does."""


def _is_lan_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        # Not global: loopback, private, link-local and the shared 100.64/10
        # range Tailscale hands out all count as the home network.
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return False


def normalize(raw: str) -> str:
    """The URL cleaned (trimmed, no trailing slash), ``""`` for off.

    Raises ``ValueError`` naming what is wrong: a scheme other than http or
    https, a host that is not loopback or a LAN address, or anything after the
    host and port.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https"):
        raise ValueError("The hub URL must start with http:// or https://.")
    try:
        host = parts.hostname
        parts.port  # noqa: B018 - reading it raises on a malformed port
    except ValueError:
        raise ValueError("The hub URL has an invalid port.") from None
    if not host or parts.username is not None or parts.password is not None:
        raise ValueError("The hub URL must be a host and optional port, such as http://127.0.0.1:8000.")
    if not _is_lan_host(host):
        raise ValueError("The hub must be on this PC or your home network (127.0.0.1, localhost or a 192.168.x.x style address).")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("The hub URL takes no path: use just the address and port.")
    return f"{parts.scheme}://{parts.netloc}"


def check(base_url: str) -> None:
    """Ask the hub's ``/healthz``. Blocking; raises ``HubUrlError`` with the
    reason when it is unreachable, errors, or is not a local-llm-hub."""
    try:
        body = _loopback_http.request(
            "GET", f"{base_url.rstrip('/')}/healthz", error=HubUrlError, service=HUB_SERVICE,
            timeout=_TIMEOUT, allow_empty=False,
        )
    except HubUrlError as exc:
        if exc.status != 503:
            raise
        # A transport failure's own text is the HTTP library's, too long for a phone.
        logger.info("⚠️ hub check failed for %s: %s", base_url, exc)
        raise HubUrlError(
            f"Nothing answered at {base_url} within {_TIMEOUT:.0f} s. Is the hub running?", status=503
        ) from exc
    if not isinstance(body, dict) or body.get("ok") is not True:
        raise HubUrlError(f"{base_url} answered, but not as a {HUB_SERVICE} (/healthz is not ok).")
