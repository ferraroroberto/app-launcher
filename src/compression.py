"""Stream-safe gzip middleware for a fleet web app.

Phone launches are dominated by bytes on the wire, so every app serves
compressed text responses. Starlette's ``GZipMiddleware`` is not enough on its
own: it only skips ``text/event-stream``, and its compressor holds small writes
back, so an MJPEG ``multipart/x-mixed-replace`` stream arrives late and JPEGs,
video and fonts are recompressed for nothing. This middleware decides from the
response headers and compresses like this:

* ``http`` scopes only: websockets pass through untouched.
* Compress only when the client accepts gzip, the response carries no
  ``Content-Encoding`` and no ``Cache-Control: no-transform``, and the body
  reaches ``minimum_size``. A 304 or any other bodyless response is under any
  minimum, so revalidation sends nothing extra.
* Never compress live streams (``text/event-stream``,
  ``multipart/x-mixed-replace``) or already-compressed media (images other than
  SVG, video, audio, archives, PDF, fonts).
* ``bypass_paths`` are path prefixes the app serves untouched (for example a
  camera stream endpoint that does not set a recognisable content type).
* A streamed response of a compressible type is flushed after every chunk
  (``Z_SYNC_FLUSH``), so no chunk is held back inside the compressor.
* A ``BaseHTTPMiddleware`` layer re-streams every body, so a tiny reply arrives
  as streamed chunks, with or without the original ``Content-Length``. A
  declared length decides at once. Without one, when the first chunk of a
  compressible, non-stream type is under ``minimum_size``, the chunks are held
  until the body ends (sent plain), reaches the minimum (gzip starts), or
  ``lookahead_seconds`` pass (a slow text stream is released and gzipped).
  Line-delimited JSON types are streams by type and are never held.

Vendor-verbatim: consuming apps copy this file byte-identical into their own
``src/`` and register it with ``app.add_middleware(StreamSafeGZipMiddleware,
bypass_paths=(...))``. Register it last so it wraps the outermost layer: an
auth failure or a bodyless response then passes through it unchanged. Requires
``starlette`` (already present in any FastAPI app); gzip only, as ``brotli``
is not in the stack.
"""

from __future__ import annotations

import asyncio
import zlib
from collections.abc import Sequence

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MIN_COMPRESS_BYTES = 1000
COMPRESS_LEVEL = 6
# Longest a small first chunk of a streamed body is held to learn whether the
# body ends under the minimum. A re-streamed tiny reply ends within one event
# loop turn; a slower body is a real stream and must not wait for the minimum.
LOOKAHEAD_SECONDS = 0.1

# Live streams: a compressor that buffers them delays every frame or event.
_STREAM_TYPES = ("text/event-stream", "multipart/x-mixed-replace")

# Line-delimited JSON is compressible but always a stream: first chunk at once.
_LINE_STREAM_TYPES = (
    "application/x-ndjson",
    "application/ndjson",
    "application/jsonl",
    "application/x-jsonlines",
    "application/stream+json",
    "application/json-seq",
)

# Media and containers that are already compressed; gzip adds CPU and no savings.
_COMPRESSED_PREFIXES = ("image/", "video/", "audio/", "font/")
_COMPRESSED_TYPES = (
    "application/zip",
    "application/gzip",
    "application/x-gzip",
    "application/x-7z-compressed",
    "application/x-rar-compressed",
    "application/pdf",
    "application/font-woff",
    "application/font-woff2",
)
# SVG is XML text and shrinks well, unlike the other ``image/`` types.
_COMPRESSIBLE_IMAGE = "image/svg+xml"


def _accepts_gzip(accept_encoding: str) -> bool:
    for part in accept_encoding.split(","):
        coding, _, params = part.partition(";")
        if coding.strip().lower() not in ("gzip", "*"):
            continue
        q = params.strip().lower()
        # ``gzip;q=0`` is an explicit refusal.
        if q.startswith("q=") and _is_zero(q[2:]):
            return False
        return True
    return False


def _is_zero(q_value: str) -> bool:
    try:
        return float(q_value) == 0.0
    except ValueError:
        return False


def _is_compressible_type(content_type: str) -> bool:
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type in _STREAM_TYPES or media_type in _COMPRESSED_TYPES:
        return False
    if media_type == _COMPRESSIBLE_IMAGE:
        return True
    return not media_type.startswith(_COMPRESSED_PREFIXES)


class StreamSafeGZipMiddleware:
    """Gzip compressible responses; pass streams, media and bypass paths through."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        minimum_size: int = MIN_COMPRESS_BYTES,
        compresslevel: int = COMPRESS_LEVEL,
        bypass_paths: Sequence[str] = (),
        lookahead_seconds: float = LOOKAHEAD_SECONDS,
    ) -> None:
        self.app = app
        self.minimum_size = minimum_size
        self.compresslevel = compresslevel
        self.lookahead_seconds = lookahead_seconds
        self.bypass_paths = tuple(bypass_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._passes_through(scope):
            await self.app(scope, receive, send)
            return
        responder = _GzipResponder(
            self.app, self.minimum_size, self.compresslevel, self.lookahead_seconds
        )
        await responder(scope, receive, send)

    def _passes_through(self, scope: Scope) -> bool:
        if self.bypass_paths and scope["path"].startswith(self.bypass_paths):
            return True
        return not _accepts_gzip(Headers(scope=scope).get("accept-encoding", ""))


class _GzipResponder:
    """One response's state: holds the start message until the body decides."""

    def __init__(
        self, app: ASGIApp, minimum_size: int, compresslevel: int, lookahead: float
    ) -> None:
        self.app = app
        self.minimum_size = minimum_size
        self.lookahead = lookahead
        self.compressor = zlib.compressobj(compresslevel, zlib.DEFLATED, 31)
        self.send: Send
        self.start: Message = {}
        self.deciding = False
        self.compressing = False
        # Body chunks merged into one message while the look-ahead runs.
        self.held: Message | None = None
        self.timer: asyncio.TimerHandle | None = None
        # The timer releases a held chunk from its own task, so sends serialise.
        self.lock = asyncio.Lock()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.send = send
        try:
            await self.app(scope, receive, self._send_response)
        finally:
            self._cancel_timer()

    async def _send_response(self, message: Message) -> None:
        async with self.lock:
            await self._handle(message)

    async def _handle(self, message: Message) -> None:
        kind = message["type"]
        if kind == "http.response.start":
            self.start = message
            self.deciding = True
            return
        if self.held is not None and kind != "http.response.body":
            await self._release_held()
        if self.held is not None:
            self._merge_held(message)
            if self._lookahead_over(self.held):
                await self._release_held()
            return
        if self.deciding:
            if self._should_hold(message):
                self.held = dict(message)
                self.timer = asyncio.get_running_loop().call_later(
                    self.lookahead, self._expire_lookahead
                )
                return
            await self._decide(message)
        elif self.compressing and kind == "http.response.body":
            self._encode_into(message)
            await self.send(message)
        else:
            await self.send(message)

    async def _decide(self, message: Message) -> None:
        self.deciding = False
        # ``pathsend`` and any other first message are never compressed.
        self.compressing = message["type"] == "http.response.body" and (
            self._should_compress(message)
        )
        if self.compressing:
            self._encode_into(message)
            self._mark_compressed(message)
        await self.send(self.start)
        await self.send(message)

    def _should_hold(self, first: Message) -> bool:
        """A small chunk of a streamed, compressible, unsized body is held."""
        if first["type"] != "http.response.body" or not first.get("more_body", False):
            return False
        if len(first.get("body", b"")) >= self.minimum_size:
            return False
        headers = Headers(raw=self.start["headers"])
        if "content-length" in headers:
            return False
        media_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        return media_type not in _LINE_STREAM_TYPES and self._eligible(headers)

    def _merge_held(self, message: Message) -> None:
        assert self.held is not None
        self.held["body"] = self.held["body"] + message.get("body", b"")
        self.held["more_body"] = message.get("more_body", False)

    def _lookahead_over(self, held: Message) -> bool:
        return (
            not held.get("more_body", False) or len(held["body"]) >= self.minimum_size
        )

    async def _release_held(self) -> None:
        self._cancel_timer()
        held, self.held = self.held, None
        assert held is not None
        await self._decide(held)

    def _expire_lookahead(self) -> None:
        asyncio.get_running_loop().create_task(self._expire())

    async def _expire(self) -> None:
        async with self.lock:
            if self.held is not None:
                await self._release_held()

    def _cancel_timer(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None

    @staticmethod
    def _eligible(headers: Headers) -> bool:
        if "content-encoding" in headers:
            return False
        if "no-transform" in headers.get("cache-control", "").lower():
            return False
        return _is_compressible_type(headers.get("content-type", ""))

    def _should_compress(self, first_body: Message) -> bool:
        if not self._eligible(Headers(raw=self.start["headers"])):
            return False
        declared = Headers(raw=self.start["headers"]).get("content-length", "")
        if declared.isdigit():
            # Authoritative even when a wrapper re-streams the body in chunks.
            return int(declared) >= self.minimum_size
        body = first_body.get("body", b"")
        return first_body.get("more_body", False) or len(body) >= self.minimum_size

    def _mark_compressed(self, first_body: Message) -> None:
        headers = MutableHeaders(raw=self.start["headers"])
        headers["Content-Encoding"] = "gzip"
        headers.add_vary_header("Accept-Encoding")
        if first_body.get("more_body", False):
            # A streamed response has no known length.
            del headers["Content-Length"]
        else:
            headers["Content-Length"] = str(len(first_body["body"]))

    def _encode_into(self, message: Message) -> None:
        more_body = message.get("more_body", False)
        out = self.compressor.compress(message.get("body", b""))
        # A sync flush emits everything seen so far, so a streamed chunk is
        # never held back inside the compressor.
        message["body"] = out + self.compressor.flush(
            zlib.Z_SYNC_FLUSH if more_body else zlib.Z_FINISH
        )
        if not more_body:
            self.compressing = False
