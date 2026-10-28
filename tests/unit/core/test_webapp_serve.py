"""``app.webapp.serve``, the one launch entry: HTTP/2 over TLS, the selector
loop, and proxy headers trusted from loopback only."""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from app.webapp import serve
from app.webapp.event_loop import selector_loop_factory


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

    monkeypatch.setitem(sys.modules, "app.webapp.server", types.SimpleNamespace(app=stub_app))
    monkeypatch.setattr(serve, "_serve_until_signalled", fake_serve)
    serve.run("127.0.0.1", 18445)

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
    assert seen["args"] == ("0.0.0.0", 8445, "c.pem", "k.pem", "info")


def test_cli_requires_a_port():
    with pytest.raises(SystemExit):
        serve.main(["--host", "127.0.0.1"])
