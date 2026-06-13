"""Shared plumbing for the loopback sibling-app HTTP clients.

``session_client``, ``voice_client`` and ``photo_ocr_client`` all talk to a
same-host sibling app over loopback and make the *same* three decisions on
every call: a transport failure (``requests.RequestException``) maps to a 503,
a ``>= 400`` upstream response surfaces with its own status (preferring the
body's ``detail``), and a non-JSON body either collapses to ``{}`` or raises.
This module owns that decision once, plus the per-call
``InsecureRequestWarning`` suppression every ``verify=False`` loopback call
would otherwise emit — so each per-service client shrinks to a list of
endpoint signatures delegating here.

Each client declares a trivial :class:`LoopbackError` subclass (e.g.
``VoiceTranscriberError``) so callers keep catching one service's failures
without catching another's; the shared ``status``-carrying ``__init__`` lives
on the base.
"""

from __future__ import annotations

import logging
from typing import Any, Type

import requests
import urllib3

logger = logging.getLogger(__name__)

# The sibling apps serve self-signed loopback certs, so every loopback call
# passes verify=False — which makes urllib3 emit a per-call
# InsecureRequestWarning that would otherwise flood the log (the connection is
# loopback-only anyway). Suppress it once here; every client inherits the
# silence by importing this module.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class LoopbackError(RuntimeError):
    """Base for the per-service loopback-client errors.

    Carries the HTTP ``status`` the webapp router re-raises to the phone
    (``HTTPException(status_code=exc.status, detail=str(exc))``).
    """

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


def _detail(resp: requests.Response, service: str) -> str:
    """The cleanest message we can surface for a ``>= 400`` response: the
    upstream body's ``detail`` field when present, else a bare status line."""
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("detail"):
            return str(body["detail"])
    except ValueError:
        pass
    return f"{service} HTTP {resp.status_code}"


def request(
    method: str,
    url: str,
    *,
    error: Type[LoopbackError],
    service: str,
    timeout: float,
    verify: bool = True,
    allow_empty: bool = True,
    **kwargs: Any,
) -> Any:
    """Make one loopback HTTP call and apply the shared error mapping.

    ``error`` is the per-service :class:`LoopbackError` subclass to raise and
    ``service`` the human label used in generated messages. A transport
    failure becomes a 503; a ``>= 400`` response is raised with its own status;
    a non-JSON body returns ``{}`` when ``allow_empty`` (the default), else
    raises a 502. ``**kwargs`` (``json``, ``params``, ``files``, ``data``,
    ``headers``, ...) flow straight to ``requests.request``.
    """
    try:
        resp = requests.request(method, url, timeout=timeout, verify=verify, **kwargs)
    except requests.RequestException as exc:
        raise error(f"{service} unreachable at {url} ({exc})", status=503) from exc
    if resp.status_code >= 400:
        raise error(_detail(resp, service), status=resp.status_code)
    try:
        return resp.json()
    except ValueError as exc:
        if allow_empty:
            return {}
        raise error(f"{service} returned non-JSON ({exc})") from exc
