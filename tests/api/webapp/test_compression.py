"""Response compression: text goes out gzipped, streams and media do not."""

from __future__ import annotations

import asyncio

import pytest
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient

from app.webapp.compression import MINIMUM_SIZE, TextGZipMiddleware, is_compressible

GZIP = {"Accept-Encoding": "gzip"}


class TestRealApp:
    def test_entry_document_is_gzipped(self, webapp_client):
        client, _, _ = webapp_client
        plain = client.get("/", headers={"Accept-Encoding": "identity"})
        packed = client.get("/", headers=GZIP)
        assert packed.status_code == 200
        assert packed.headers["content-encoding"] == "gzip"
        assert "accept-encoding" in packed.headers["vary"].lower()
        assert int(packed.headers["content-length"]) < len(plain.content) / 2
        assert packed.content == plain.content  # httpx un-gzips for us

    def test_large_static_script_is_gzipped(self, webapp_client):
        client, _, _ = webapp_client
        plain = client.get("/static/vendor/xterm.js", headers={"Accept-Encoding": "identity"})
        packed = client.get("/static/vendor/xterm.js", headers=GZIP)
        assert plain.status_code == 200
        assert "content-encoding" not in plain.headers
        assert packed.headers["content-encoding"] == "gzip"
        assert int(packed.headers["content-length"]) < len(plain.content) / 2
        assert packed.headers["cache-control"] == plain.headers["cache-control"]

    def test_client_without_gzip_gets_identity(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.get("/", headers={"Accept-Encoding": "identity"})
        assert "content-encoding" not in resp.headers

    def test_small_json_stays_uncompressed(self, webapp_client):
        # Pins the middleware order: outside the auth gate, minimum_size is
        # ignored and even a 50-byte body is gzipped.
        client, _, _ = webapp_client
        resp = client.get("/healthz", headers=GZIP)
        assert resp.status_code == 200
        assert "content-encoding" not in resp.headers

    def test_not_modified_stays_bodyless(self, webapp_client):
        client, _, _ = webapp_client
        first = client.get("/", headers=GZIP)
        etag = first.headers["etag"]
        again = client.get("/", headers={**GZIP, "If-None-Match": etag})
        assert again.status_code == 304
        assert again.content == b""
        assert "content-encoding" not in again.headers


def _stream_app(content_type: str, tail_gate: asyncio.Event):
    """An ASGI app that sends one chunk, then holds the second until released."""

    async def app(scope, receive, send):
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", content_type.encode())],
        })
        await send({"type": "http.response.body", "body": b"x" * 2000, "more_body": True})
        await tail_gate.wait()
        await send({"type": "http.response.body", "body": b"y" * 2000, "more_body": False})

    return TextGZipMiddleware(app, minimum_size=MINIMUM_SIZE)


async def _first_chunk(content_type: str):
    """Messages the client has seen when the app is still holding its tail."""
    gate = asyncio.Event()
    seen: list[dict] = []
    first_body = asyncio.Event()

    async def send(message):
        seen.append(message)
        if message["type"] == "http.response.body":
            first_body.set()

    async def receive():
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    scope = {
        "type": "http", "method": "GET", "path": "/", "query_string": b"",
        "headers": [(b"accept-encoding", b"gzip")],
    }
    task = asyncio.create_task(_stream_app(content_type, gate)(scope, receive, send))
    try:
        await asyncio.wait_for(first_body.wait(), timeout=2)
        return list(seen)
    finally:
        gate.set()
        await task


@pytest.mark.parametrize("content_type", ["text/event-stream", "audio/L16"])
def test_streams_pass_through_immediately(content_type):
    seen = asyncio.run(_first_chunk(content_type))
    start, body = seen[0], seen[1]
    assert (b"content-encoding", b"gzip") not in start["headers"]
    assert body["body"] == b"x" * 2000


def test_text_stream_is_compressed_in_chunks():
    seen = asyncio.run(_first_chunk("text/plain"))
    assert (b"content-encoding", b"gzip") in seen[0]["headers"]


@pytest.mark.parametrize("content_type", ["image/png", "image/jpeg", "application/pdf", "audio/L16"])
def test_precompressed_media_is_left_alone(content_type):
    async def endpoint(request):
        return Response(b"\x00" * 5000, media_type=content_type)

    app = Starlette(routes=[Route("/m", endpoint)])
    app.add_middleware(TextGZipMiddleware, minimum_size=MINIMUM_SIZE)
    resp = TestClient(app).get("/m", headers=GZIP)
    assert "content-encoding" not in resp.headers
    assert resp.content == b"\x00" * 5000


def test_websocket_is_untouched():
    async def echo(websocket):
        await websocket.accept()
        await websocket.send_bytes(b"x" * 5000)
        await websocket.close()

    app = Starlette(routes=[WebSocketRoute("/ws", echo)])
    app.add_middleware(TextGZipMiddleware, minimum_size=MINIMUM_SIZE)
    with TestClient(app).websocket_connect("/ws", headers=GZIP) as ws:
        assert ws.receive_bytes() == b"x" * 5000


@pytest.mark.parametrize("content_type, expected", [
    ("text/html; charset=utf-8", True),
    ("text/javascript", True),
    ("text/css", True),
    ("application/json", True),
    ("image/svg+xml", True),
    ("application/manifest+json", True),
    ("text/event-stream", False),
    ("image/png", False),
    ("audio/L16", False),
    ("application/octet-stream", False),
    ("", False),
])
def test_is_compressible(content_type, expected):
    assert is_compressible(content_type) is expected

