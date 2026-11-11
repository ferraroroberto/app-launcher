"""``app.webapp.serve``, the one launch entry: HTTP/2 over TLS, the selector
loop, and proxy headers trusted from loopback only. The module is the
scaffold's vendored copy; these pin how this app runs it."""

from __future__ import annotations

import asyncio
import sys
import types

import pytest
from hypercorn.protocol.h2 import H2Protocol

from app.webapp import event_loop, serve
from app.webapp.event_loop import LOOP_FACTORY, selector_loop_factory


def test_tls_config_offers_http2_first_then_http11():
    cfg = serve.build_config("127.0.0.1", 18445, "cert.pem", "key.pem")
    assert cfg.bind == ["127.0.0.1:18445"]
    assert (cfg.certfile, cfg.keyfile) == ("cert.pem", "key.pem")
    # HTTP/1.1 stays offered: cloudflared and the tray's probes speak it.
    assert cfg.alpn_protocols == ["h2", "http/1.1"]


def test_plain_http_config_has_no_tls():
    cfg = serve.build_config("127.0.0.1", 18445)
    assert cfg.certfile is None and cfg.keyfile is None


def test_websocket_pings_keep_the_interval_the_webapp_ran_with():
    assert serve.build_config("127.0.0.1", 18445).websocket_ping_interval == 20.0


async def _client_seen(client: tuple, forwarded_for: str) -> tuple:
    seen = {}

    async def app(scope, receive, send):
        seen["client"] = tuple(scope["client"])

    scope = {
        "type": "http",
        "scheme": "https",
        "client": client,
        "headers": [(b"x-forwarded-for", forwarded_for.encode())],
    }
    await serve.wrap_app(app)(scope, None, None)
    return seen["client"]


def test_a_loopback_proxy_names_the_real_client():
    """cloudflared dials over loopback, so its X-Forwarded-For is believed and
    the bearer gate sees a remote caller, not the PC."""
    client = asyncio.run(_client_seen(("127.0.0.1", 50000), "203.0.113.9"))
    assert client[0] == "203.0.113.9"


def test_a_remote_caller_cannot_claim_to_be_loopback():
    """A tailnet caller's own X-Forwarded-For is ignored, or it could claim
    127.0.0.1 and skip the bearer gate as the PC."""
    client = asyncio.run(_client_seen(("100.64.0.7", 50000), "127.0.0.1"))
    assert client == ("100.64.0.7", 50000)


def test_run_serves_the_app_on_the_selector_loop(monkeypatch):
    stub_app = object()
    seen = {}

    async def fake_serve(app, cfg):
        seen["app"] = app
        seen["loop"] = type(asyncio.get_running_loop())
        seen["bind"] = cfg.bind

    async def unpatched_handle(self, event):
        return None

    monkeypatch.setitem(sys.modules, "app.webapp.server", types.SimpleNamespace(app=stub_app))
    monkeypatch.setattr(serve, "_serve_until_signalled", fake_serve)
    monkeypatch.setattr(H2Protocol, "handle", unpatched_handle)
    serve.run("127.0.0.1", 18445, loop_factory=selector_loop_factory)
    assert getattr(H2Protocol.handle, "_releases_streams", False), (
        "every launch must serve with abandoned h2 streams released")

    loop = selector_loop_factory()
    try:
        assert seen["loop"] is type(loop)
    finally:
        loop.close()
    assert seen["bind"] == ["127.0.0.1:18445"]
    assert seen["app"].app is stub_app, "the app must be served behind the proxy-header wrapper"


def test_cli_passes_uvicorn_style_flags_through(monkeypatch):
    seen = {}
    monkeypatch.setattr(serve, "run", lambda *a: seen.setdefault("args", a))
    assert serve.main([
        "--host", "0.0.0.0", "--port", "8445",
        "--ssl-keyfile", "k.pem", "--ssl-certfile", "c.pem", "--log-level", "info",
    ]) == 0
    assert seen["args"] == (
        "0.0.0.0", 8445, "c.pem", "k.pem", "info", "hypercorn",
        "app.webapp.server:app", serve.selector_loop_factory,
    )


def test_cli_loop_flag_resolves_to_the_gil_tuned_factory(monkeypatch):
    """The launch paths pass ``--loop LOOP_FACTORY``; it must name the factory
    that tunes the GIL switch interval, not the vendored module's bare one."""
    seen = {}
    monkeypatch.setattr(serve, "run", lambda *a: seen.setdefault("args", a))
    assert serve.main(["--port", "8445", "--loop", LOOP_FACTORY]) == 0
    assert seen["args"][-1] is event_loop.selector_loop_factory


def test_cli_selects_the_uvicorn_fallback(monkeypatch):
    seen = {}
    monkeypatch.setattr(serve, "run", lambda *a: seen.setdefault("args", a))
    assert serve.main(["--port", "8445", "--server", "uvicorn"]) == 0
    assert seen["args"][5] == "uvicorn"


def test_cli_rejects_an_unknown_server():
    with pytest.raises(SystemExit):
        serve.main(["--port", "8445", "--server", "daphne"])


def test_uvicorn_fallback_config_matches_the_hypercorn_one():
    ucfg = serve.build_uvicorn_config(object(), "127.0.0.1", 18445, "cert.pem", "key.pem", "WARNING")
    assert (ucfg.host, ucfg.port) == ("127.0.0.1", 18445)
    assert (ucfg.ssl_certfile, ucfg.ssl_keyfile) == ("cert.pem", "key.pem")
    assert ucfg.ws_ping_interval == serve.WS_PING_INTERVAL_S
    assert ucfg.log_level == "warning"
    # wrap_app already trusts loopback only; uvicorn's own pass would apply it twice.
    assert ucfg.proxy_headers is False


def test_run_serves_the_uvicorn_fallback_on_the_selector_loop(monkeypatch):
    stub_app = object()
    seen = {}

    async def fake_uvicorn(ucfg):
        seen["app"] = ucfg.app
        seen["loop"] = type(asyncio.get_running_loop())
        seen["bind"] = (ucfg.host, ucfg.port)

    async def no_hypercorn(*_a):
        raise AssertionError("the uvicorn fallback must not start hypercorn")

    monkeypatch.setitem(sys.modules, "app.webapp.server", types.SimpleNamespace(app=stub_app))
    monkeypatch.setattr(serve, "_serve_uvicorn", fake_uvicorn)
    monkeypatch.setattr(serve, "_serve_until_signalled", no_hypercorn)
    serve.run("127.0.0.1", 18445, server="uvicorn", loop_factory=selector_loop_factory)

    loop = selector_loop_factory()
    try:
        assert seen["loop"] is type(loop)
    finally:
        loop.close()
    assert seen["bind"] == ("127.0.0.1", 18445)
    assert seen["app"].app is stub_app, "the fallback must serve behind the proxy-header wrapper"


def test_run_rejects_an_unknown_server():
    with pytest.raises(ValueError, match="daphne"):
        serve.run("127.0.0.1", 18445, server="daphne")


def test_cli_requires_a_port():
    with pytest.raises(SystemExit):
        serve.main(["--host", "127.0.0.1"])


async def _abandoned_h2_responses_that_finished(clients: int) -> int:
    """Serve over h2c, let each client read part of a large body and vanish
    without opening its flow-control window; count the ASGI calls that end."""
    import socket

    import h2.config
    import h2.connection
    from hypercorn.asyncio import serve as hypercorn_serve
    from hypercorn.config import Config

    finished = []

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        await send({"type": "http.response.start", "status": 200, "headers": []})
        # Past the 65,535-byte initial window, so the end of it can never be sent.
        await send({"type": "http.response.body", "body": b"x" * 300_000})
        finished.append(scope["path"])

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    cfg = Config()
    cfg.bind = [f"127.0.0.1:{port}"]
    cfg.loglevel = "WARNING"
    stop = asyncio.Event()
    server = asyncio.create_task(hypercorn_serve(app, cfg, shutdown_trigger=stop.wait))
    try:
        for _ in range(100):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                break
            except OSError:
                await asyncio.sleep(0.05)
        else:
            raise AssertionError("hypercorn never listened")
        writer.close()
        for n in range(clients):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            conn = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
            conn.initiate_connection()
            conn.send_headers(1, [(":method", "GET"), (":path", f"/{n}"),
                                  (":scheme", "http"), (":authority", "t")], end_stream=True)
            writer.write(conn.data_to_send())
            await writer.drain()
            await reader.read(70_000)
            writer.close()
        deadline = asyncio.get_running_loop().time() + 5
        while len(finished) < clients and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        return len(finished)
    finally:
        stop.set()
        server.cancel()
        try:
            await server
        except BaseException:
            pass


async def _unanswered_h2_requests(total: int, batch: int) -> list:
    """Serve over h2c with the launch config and send ``total`` requests on one
    connection, ``batch`` in flight at a time; the streams that never got an answer."""
    import socket

    import h2.config
    import h2.connection
    import h2.events
    from hypercorn.asyncio import serve as hypercorn_serve

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    stop = asyncio.Event()
    server = asyncio.create_task(
        hypercorn_serve(app, serve.build_config("127.0.0.1", port), shutdown_trigger=stop.wait))
    try:
        for _ in range(100):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                break
            except OSError:
                await asyncio.sleep(0.05)
        else:
            raise AssertionError("hypercorn never listened")
        conn = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
        conn.initiate_connection()
        sent = 0
        while sent < total:
            pending = set()
            for _ in range(min(batch, total - sent)):
                stream_id = conn.get_next_available_stream_id()
                conn.send_headers(stream_id, [(":method", "GET"), (":path", f"/{sent}"),
                                              (":scheme", "http"), (":authority", "t")], end_stream=True)
                pending.add(stream_id)
                sent += 1
            writer.write(conn.data_to_send())
            await writer.drain()
            while pending:
                try:
                    # A healthy answer takes milliseconds; the stall is the 5 s keep-alive.
                    data = await asyncio.wait_for(reader.read(65_536), 2)
                except asyncio.TimeoutError:
                    return sorted(pending)
                if not data:
                    return sorted(pending)
                for event in conn.receive_data(data):
                    if isinstance(event, h2.events.StreamEnded):
                        pending.discard(event.stream_id)
                writer.write(conn.data_to_send())
        writer.close()
        return []
    finally:
        stop.set()
        server.cancel()
        try:
            await server
        except BaseException:
            pass


def test_every_request_on_a_long_lived_h2_connection_is_answered():
    """A page opened late in a browser session shares the connection every
    earlier page booted over. Past hypercorn's default per-connection request
    cap, the requests in flight must still be answered at once, not left for
    the keep-alive timer to drop."""
    from hypercorn.config import Config

    total = Config.keep_alive_max_requests + 200
    assert asyncio.run(_unanswered_h2_requests(total, batch=20)) == []


def test_a_response_whose_h2_client_vanished_still_finishes(monkeypatch):
    """A client that leaves mid-response must not leave its ASGI call waiting
    forever, or every closed tab leaks a task and its connection."""
    # Registered so the patch below is undone after the test.
    monkeypatch.setattr(H2Protocol, "handle", H2Protocol.handle)
    serve.release_h2_streams_on_disconnect()
    assert asyncio.run(_abandoned_h2_responses_that_finished(3)) == 3
