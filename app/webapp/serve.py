"""Serve a fleet web app over HTTP/2 (with HTTP/1.1 fallback): the one way it is launched.

A cold phone boot fetches ~130 resources. Over HTTP/1.1 the browser caps a host
at six connections, so most of them queue behind each other; over HTTP/2 they
share one multiplexed connection. On the perf-review phone profile that took the
cold load event from ~2.3 s to ~1.0 s with the same build and no bundling step.
Browsers only speak HTTP/2 over TLS, so the gain needs ``--ssl-certfile`` and
``--ssl-keyfile``; without them the server is plain HTTP/1.1 either way.

Every launch path (the tray's spawn, ``webapp.bat``, a CLI command, the e2e
autoboot) goes through here so a server setting lands everywhere at once::

    python -m app.webapp.serve --host H --port P [--ssl-keyfile K --ssl-certfile C]
        [--log-level L] [--server hypercorn|uvicorn]
        [--app module:attr] [--loop module:factory]

The flag names are uvicorn's. ``--app`` is the ASGI app (default
``app.webapp.server:app``); ``--loop`` is an event-loop factory, as uvicorn's
``--loop`` takes (default: the Windows selector loop, which a client aborting
mid-handshake cannot wedge, unlike the proactor default).

``--server uvicorn`` is the fallback if HTTP/2 misbehaves: the HTTP/1.1 server
the app ran on before, with the same loop, proxy trust, TLS and ping interval.
It imports nothing from hypercorn, so it works where hypercorn is not installed.

Vendor-verbatim: consuming apps copy this file byte-identical to
``app/webapp/serve.py``. The per-app parts are the flags above, never an edit.
Needs ``uvicorn`` always and ``hypercorn>=0.18`` for the default server.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import logging
import signal
import sys
from collections.abc import Callable
from typing import Any

import uvicorn
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

logger = logging.getLogger(__name__)

DEFAULT_APP = "app.webapp.server:app"
# cloudflared runs on this PC, dials the app over loopback and names the real
# client in X-Forwarded-For. Only a loopback peer may say so; a tailnet caller
# sending the header must not be able to pose as the PC.
TRUSTED_PROXY_HOSTS = "127.0.0.1"
# Server-side pings find a suspended phone's dead socket; 20 s is the interval
# the apps ran with under uvicorn.
WS_PING_INTERVAL_S = 20.0
HYPERCORN = "hypercorn"
UVICORN = "uvicorn"
SERVERS = (HYPERCORN, UVICORN)
ALPN_PROTOCOLS = ["h2", "http/1.1"]


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    """The default loop: Windows' selector loop has no accept-wedge on an aborted client."""
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()


def load_attr(target: str) -> Any:
    """Resolve ``package.module:attr`` (the form uvicorn takes), importing the module."""
    module_name, sep, attr = target.partition(":")
    if not sep or not module_name or not attr:
        raise ValueError(f"❌ {target!r} is not of the form 'package.module:attribute'")
    obj: Any = importlib.import_module(module_name)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


def build_config(
    host: str,
    port: int,
    certfile: str | None = None,
    keyfile: str | None = None,
    log_level: str = "warning",
) -> Any:
    """The hypercorn config every launch path shares (a ``hypercorn.config.Config``).

    TLS is what enables HTTP/2: browsers only speak it over TLS, negotiated by
    ALPN. Hypercorn is imported here, not at module load, so the uvicorn
    fallback runs without it.
    """
    from hypercorn.config import Config

    cfg = Config()
    cfg.bind = [f"{host}:{port}"]
    cfg.loglevel = log_level.upper()
    cfg.websocket_ping_interval = WS_PING_INTERVAL_S
    if certfile and keyfile:
        cfg.certfile = certfile
        cfg.keyfile = keyfile
        cfg.alpn_protocols = list(ALPN_PROTOCOLS)
    return cfg


def build_uvicorn_config(
    app: Any,
    host: str,
    port: int,
    certfile: str | None = None,
    keyfile: str | None = None,
    log_level: str = "warning",
) -> uvicorn.Config:
    """The HTTP/1.1 fallback's config. ``app`` is already behind :func:`wrap_app`,
    so uvicorn's own proxy-header handling stays off rather than applying the
    same trust twice."""
    return uvicorn.Config(
        app,
        host=host,
        port=port,
        ssl_certfile=certfile,
        ssl_keyfile=keyfile,
        log_level=log_level.lower(),
        ws_ping_interval=WS_PING_INTERVAL_S,
        proxy_headers=False,
    )


def release_h2_streams_on_disconnect() -> None:
    """Let a response finish when its HTTP/2 client has gone. Idempotent.

    Hypercorn 0.18 stops its send task when a connection closes but never
    closes the stream buffers still holding unsent data, so every response the
    client abandoned mid-send waits in ``drain()`` forever: its ASGI call and
    the connection's tasks never end. A closed tab or a phone that drops
    mid-load leaks one each; an e2e run that closes a browser context per test
    piled up ~600 per app. Closing the buffers on ``Closed`` wakes those waits
    so the call returns.
    """
    from hypercorn.events import Closed
    from hypercorn.protocol.h2 import H2Protocol

    if getattr(H2Protocol.handle, "_releases_streams", False):
        return
    original = H2Protocol.handle

    async def handle_then_release_streams(self: Any, event: Any) -> None:
        await original(self, event)
        if isinstance(event, Closed):
            for buffer in list(self.stream_buffers.values()):
                await buffer.close()

    handle_then_release_streams._releases_streams = True  # type: ignore[attr-defined]
    setattr(H2Protocol, "handle", handle_then_release_streams)


def wrap_app(app: Any) -> Any:
    """The ASGI app as served: client address taken from X-Forwarded-For only
    when the peer is loopback, which an app's bearer and terminal gates read."""
    return ProxyHeadersMiddleware(app, trusted_hosts=TRUSTED_PROXY_HOSTS)


async def _serve_until_signalled(app: Any, cfg: Any) -> None:
    """Serve until SIGINT/SIGTERM/SIGBREAK, then shut down gracefully so the
    app's lifespan shutdown runs (the tray stops the app with CTRL_BREAK)."""
    from hypercorn.asyncio import serve

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _on_signal(*_args: Any) -> None:
        loop.call_soon_threadsafe(stop.set)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, _on_signal)
    await serve(app, cfg, shutdown_trigger=stop.wait)


async def _serve_uvicorn(ucfg: uvicorn.Config) -> None:
    """uvicorn handles SIGINT/SIGTERM/SIGBREAK itself and runs the lifespan
    shutdown, so the tray's CTRL_BREAK stops it the same way."""
    await uvicorn.Server(ucfg).serve()


def run(
    host: str,
    port: int,
    certfile: str | None = None,
    keyfile: str | None = None,
    log_level: str = "warning",
    server: str = HYPERCORN,
    app_path: str = DEFAULT_APP,
    loop_factory: Callable[[], asyncio.AbstractEventLoop] = selector_loop_factory,
) -> None:
    """Serve ``app_path`` on the loop ``loop_factory`` builds until signalled."""
    if server not in SERVERS:
        raise ValueError(f"❌ unknown server {server!r}; expected one of {', '.join(SERVERS)}")
    scheme = "https" if certfile and keyfile else "http"
    if server == HYPERCORN:
        try:
            cfg = build_config(host, port, certfile, keyfile, log_level)
        except ImportError as exc:
            raise RuntimeError(
                f"❌ hypercorn is not installed ({exc}): pip install -r requirements.txt, "
                f"or launch with --server {UVICORN} for HTTP/1.1"
            ) from exc
        release_h2_streams_on_disconnect()
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        # The loop exists, and a factory's process-wide tuning is in force,
        # before the app's module-level setup runs.
        runner.get_loop()
        app = load_attr(app_path)
        if server == UVICORN:
            logger.warning(f"⚠️ serving {app_path} on {scheme}://{host}:{port} with the uvicorn fallback (http/1.1 only)")
            runner.run(_serve_uvicorn(build_uvicorn_config(wrap_app(app), host, port, certfile, keyfile, log_level)))
            return
        logger.info(f"ℹ️ serving {app_path} on {scheme}://{host}:{port} ({', '.join(cfg.alpn_protocols)})")
        runner.run(_serve_until_signalled(wrap_app(app), cfg))


def main(argv: list[str] | None = None) -> int:
    doc = __doc__ or ""
    ap = argparse.ArgumentParser(prog="python -m app.webapp.serve", description=doc.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--ssl-certfile", default=None)
    ap.add_argument("--ssl-keyfile", default=None)
    ap.add_argument("--log-level", default="warning")
    ap.add_argument("--server", choices=SERVERS, default=HYPERCORN)
    ap.add_argument("--app", default=DEFAULT_APP, help="ASGI app as module:attr")
    ap.add_argument("--loop", default=None, help="event-loop factory as module:attr (default: selector loop)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=a.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    factory = load_attr(a.loop) if a.loop else selector_loop_factory
    run(a.host, a.port, a.ssl_certfile, a.ssl_keyfile, a.log_level, a.server, a.app, factory)
    return 0


if __name__ == "__main__":
    sys.exit(main())
