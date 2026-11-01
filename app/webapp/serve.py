"""Serve the webapp over HTTP/2 (with HTTP/1.1 fallback) — the one way it is launched.

A cold phone boot fetches ~130 resources. Over HTTP/1.1 the browser caps a
host at six connections, so most of them queued behind each other; over
HTTP/2 they share one multiplexed connection. On the perf-review phone
profile that took the cold load event from ~2.3 s to ~1.0 s with the same
build and no bundling step.

Every launch path goes through here (``WebappManager._build_command`` for the
tray and the named tunnel, ``webapp.bat``, the ``webapp`` CLI command, the e2e
autoboot and the design-review boot), so a server setting lands everywhere at
once. ``python -m app.webapp.serve --host H --port P [--ssl-keyfile K
--ssl-certfile C] [--log-level L]`` keeps uvicorn's flag names.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from typing import Any, List, Optional

from hypercorn.asyncio import serve
from hypercorn.config import Config
from hypercorn.events import Closed
from hypercorn.protocol.h2 import H2Protocol
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.webapp.event_loop import selector_loop_factory

logger = logging.getLogger(__name__)

APP_IMPORT_PATH = "app.webapp.server:app"
# cloudflared runs on this PC, dials the webapp over loopback and names the
# real client in X-Forwarded-For. Only a loopback peer may say so; a tailnet
# caller sending the header must not be able to pose as the PC.
TRUSTED_PROXY_HOSTS = "127.0.0.1"
# Server-side pings find a suspended phone's dead terminal socket; 20 s is
# the interval the webapp ran with under uvicorn.
WS_PING_INTERVAL_S = 20.0


def build_config(
    host: str,
    port: int,
    certfile: Optional[str] = None,
    keyfile: Optional[str] = None,
    log_level: str = "warning",
) -> Config:
    """The hypercorn config every launch path shares. TLS is what enables
    HTTP/2: browsers only speak it over TLS, negotiated by ALPN."""
    cfg = Config()
    cfg.bind = [f"{host}:{port}"]
    cfg.loglevel = log_level.upper()
    cfg.websocket_ping_interval = WS_PING_INTERVAL_S
    if certfile and keyfile:
        cfg.certfile = certfile
        cfg.keyfile = keyfile
        cfg.alpn_protocols = ["h2", "http/1.1"]
    return cfg


_HYPERCORN_H2_HANDLE = H2Protocol.handle


async def _handle_then_release_streams(self: H2Protocol, event: Any) -> None:
    await _HYPERCORN_H2_HANDLE(self, event)
    if isinstance(event, Closed):
        for buffer in list(self.stream_buffers.values()):
            await buffer.close()


def release_h2_streams_on_disconnect() -> None:
    """Let a response finish when its HTTP/2 client has gone.

    Hypercorn 0.18 stops its send task when a connection closes but never
    closes the stream buffers still holding unsent data, so every response
    the client abandoned mid-send waits in ``drain()`` forever: its ASGI call
    and the connection's tasks never end. A closed browser tab or a phone
    that drops mid-load leaks one each; the e2e gate, which closes a browser
    context per test, piled up ~600 per webapp in a run. Closing the buffers
    on ``Closed`` wakes those waits so the call returns. Idempotent.
    """
    H2Protocol.handle = _handle_then_release_streams


def wrap_app(app: Any) -> Any:
    """The ASGI app as served: client address taken from X-Forwarded-For only
    when the peer is loopback, which the bearer and terminal gates read."""
    return ProxyHeadersMiddleware(app, trusted_hosts=TRUSTED_PROXY_HOSTS)


async def _serve_until_signalled(app: Any, cfg: Config) -> None:
    """Serve until SIGINT/SIGTERM/SIGBREAK, then shut down gracefully so the
    app's lifespan shutdown runs (the tray stops the webapp with CTRL_BREAK)."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _on_signal(*_args: Any) -> None:
        loop.call_soon_threadsafe(stop.set)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, _on_signal)
    await serve(app, cfg, shutdown_trigger=stop.wait)


def run(
    host: str,
    port: int,
    certfile: Optional[str] = None,
    keyfile: Optional[str] = None,
    log_level: str = "warning",
) -> None:
    """Serve ``app.webapp.server:app`` on the selector loop until signalled."""
    cfg = build_config(host, port, certfile, keyfile, log_level)
    release_h2_streams_on_disconnect()
    with asyncio.Runner(loop_factory=selector_loop_factory) as runner:
        # The loop exists, and the factory's GIL tuning is in force, before
        # the app's module-level setup runs.
        runner.get_loop()
        from app.webapp.server import app

        scheme = "https" if cfg.ssl_enabled else "http"
        logger.info(f"ℹ️ serving {APP_IMPORT_PATH} on {scheme}://{host}:{port} ({', '.join(cfg.alpn_protocols)})")
        runner.run(_serve_until_signalled(wrap_app(app), cfg))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.webapp.serve", description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--ssl-certfile", default=None)
    ap.add_argument("--ssl-keyfile", default=None)
    ap.add_argument("--log-level", default="warning")
    a = ap.parse_args(argv)
    run(a.host, a.port, a.ssl_certfile, a.ssl_keyfile, a.log_level)
    return 0


if __name__ == "__main__":
    sys.exit(main())
