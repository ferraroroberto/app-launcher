"""The real app serves text gzipped and its live streams chunk by chunk."""

from __future__ import annotations

import asyncio

import pytest

GZIP = {"Accept-Encoding": "gzip"}
IDENTITY = {"Accept-Encoding": "identity"}


def _wire_bytes(client, path: str) -> tuple[int, bytes]:
    """Status and the bytes as sent for a gzip client (no decoding)."""
    with client.stream("GET", path, headers=GZIP) as resp:
        return resp.status_code, b"".join(resp.iter_raw())


class TestTextIsGzipped:
    def test_entry_document_is_gzipped(self, webapp_client):
        client, _, _ = webapp_client
        plain = client.get("/", headers=IDENTITY)
        packed = client.get("/", headers=GZIP)
        status, wire = _wire_bytes(client, "/")
        assert packed.status_code == status == 200
        assert "content-encoding" not in plain.headers
        assert packed.headers["content-encoding"] == "gzip"
        assert "accept-encoding" in packed.headers["vary"].lower()
        assert len(wire) < len(plain.content) / 2
        assert packed.content == plain.content  # httpx un-gzips for us

    def test_large_static_script_is_gzipped(self, webapp_client):
        client, _, _ = webapp_client
        plain = client.get("/static/vendor/xterm.js", headers=IDENTITY)
        packed = client.get("/static/vendor/xterm.js", headers=GZIP)
        _, wire = _wire_bytes(client, "/static/vendor/xterm.js")
        assert plain.status_code == 200
        assert packed.headers["content-encoding"] == "gzip"
        assert len(wire) < len(plain.content) / 2
        assert packed.headers["cache-control"] == plain.headers["cache-control"]

    def test_client_without_gzip_gets_identity(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.get("/", headers=IDENTITY)
        assert "content-encoding" not in resp.headers

    def test_not_modified_stays_bodyless(self, webapp_client):
        client, _, _ = webapp_client
        etag = client.get("/", headers=GZIP).headers["etag"]
        again = client.get("/", headers={**GZIP, "If-None-Match": etag})
        assert again.status_code == 304
        assert again.content == b""
        assert "content-encoding" not in again.headers


async def _first_chunk(app, path: str):
    """Messages the client has seen while the endpoint still holds its tail."""
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
        "type": "http", "method": "GET", "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": [(b"accept-encoding", b"gzip")],
        "client": ("testclient", 50000), "server": ("testserver", 80),
        "scheme": "http", "root_path": "", "http_version": "1.1",
        "state": {},
    }
    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(first_body.wait(), timeout=2)
        return list(seen)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "content_type, headers",
    [
        ("text/event-stream", {"Cache-Control": "no-store"}),
        ("audio/L16", {"Cache-Control": "no-store", "X-Sample-Rate": "24000"}),
        ("multipart/x-mixed-replace; boundary=frame", {}),
    ],
)
def test_streams_deliver_first_chunk_at_once(webapp_client, content_type, headers):
    """A stream type served through the whole stack (auth gate included)
    reaches the client before its second chunk exists."""
    from starlette.responses import StreamingResponse

    _, app, _ = webapp_client
    tail_gate = asyncio.Event()

    async def source():
        yield b"x" * 2000
        await tail_gate.wait()
        yield b"y" * 2000

    @app.get("/__stream_probe")
    async def stream_probe() -> StreamingResponse:
        return StreamingResponse(source(), media_type=content_type, headers=headers)

    seen = asyncio.run(_first_chunk(app, "/__stream_probe"))
    start, body = seen[0], seen[1]
    assert (b"content-encoding", b"gzip") not in start["headers"]
    assert body["body"] == b"x" * 2000
