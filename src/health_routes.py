"""The two routes every fleet web app answers: ``GET /healthz`` and ``GET /api/version``.

``/healthz`` is the liveness probe the tray watchdog and the launcher's readiness
probe read: a cheap ``200`` with no I/O, open to every caller because it says
nothing about the build. ``/api/version`` is the build identity
(``{git_sha, built_at}``) the restart check compares with ``HEAD``; a ``/healthz``
200 passes on a stale process, a matching ``git_sha`` does not.

    from src.health_routes import health_router

    app.include_router(health_router(service="my-app", authorize=is_signed_in))

``identity`` defaults to :func:`src.build_info.build_identity`, captured once when
the router is built (module load of the app), so a long-running process keeps
reporting the code it started with. ``authorize(request)`` is the app's own auth
check, called for ``/api/version`` only: it returns truthy to allow, falsy for a
``401``, or raises its own ``HTTPException``; it may be ``async``. A loopback
caller with no proxy header skips it, so the tray and the restart check need no
token. A request Tailscale serve or Funnel forwards also connects from loopback,
but carries ``X-Forwarded-For`` or ``Tailscale-Funnel-Request``, so it is
treated as remote. With no ``authorize`` the route is open, for an app with no
auth of its own.

``extra`` adds fixed fields to the ``/api/version`` payload (an app's own
``version`` string); ``git_sha`` and ``built_at`` always win. An app whose own
``/api/version`` already serves a richer payload at the same path passes
``version=False`` and mounts only ``/healthz``; its route must keep returning
``git_sha`` and ``built_at``.

Vendor-verbatim: adopters copy this file byte-identical into their own ``src/``
beside ``src/build_info.py`` (manifest key ``health_routes``, which requires
``build_info``). Needs FastAPI. Python 3.9.
"""

from __future__ import annotations

import inspect
import ipaddress
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Union

from fastapi import APIRouter, HTTPException, Request

from src.build_info import build_identity

#: Headers a reverse proxy on this machine sets; their presence means the
#: loopback peer is the proxy, not the caller.
PROXY_HEADERS = ("x-forwarded-for", "forwarded", "tailscale-funnel-request")

Authorize = Callable[[Request], Union[bool, Awaitable[bool]]]  # noqa: UP007  # evaluated at import on Python 3.9


def is_local_caller(peer: str | None, headers: Mapping[str, str]) -> bool:
    """Whether a request comes straight from this machine, not through a proxy.

    ``headers`` is looked up with lower-case names, as Starlette's ``Headers``
    does; pass a plain ``dict`` with lower-case keys in a test.
    """
    try:
        ip = ipaddress.ip_address((peer or "").strip())
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback and not any(headers.get(name) is not None for name in PROXY_HEADERS)


def health_router(
    *,
    service: str,
    identity: Mapping[str, str] | None = None,
    authorize: Authorize | None = None,
    version: bool = True,
    extra: Mapping[str, Any] | None = None,
) -> APIRouter:
    """A router serving ``GET /healthz`` and (unless ``version=False``) ``GET /api/version``."""
    router = APIRouter()
    liveness = {"ok": True, "service": service}

    @router.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, Any]:
        return liveness

    if not version:
        return router

    payload: dict[str, Any] = {**(extra or {}), **dict(identity if identity is not None else build_identity())}

    @router.get("/api/version")
    async def api_version(request: Request) -> dict[str, Any]:
        if authorize is not None and not is_local_caller(request.client.host if request.client else None,
                                                         request.headers):
            allowed = authorize(request)
            if inspect.isawaitable(allowed):
                allowed = await allowed
            if not allowed:
                raise HTTPException(status_code=401, detail="not signed in")
        return payload

    return router
