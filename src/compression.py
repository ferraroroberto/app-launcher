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

Vendor-verbatim: consuming apps copy this file byte-identical into their own
``src/`` and register it with ``app.add_middleware(StreamSafeGZipMiddleware,
bypass_paths=(...))``. Register it last so it wraps the outermost layer: an
auth failure or a bodyless response then passes through it unchanged. Requires
``starlette`` (already present in any FastAPI app); gzip only, as ``brotli``
is not in the stack.
"""

from __future__ import annotations

import zlib
from collections.abc import Sequence

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MIN_COMPRESS_BYTES = 1000
COMPRESS_LEVEL = 6

# Live streams: a compressor that buffers them delays every frame or event.
_STREAM_TYPES = ("text/event-stream", "multipart/x-mixed-replace")

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
    ) -> None:
        self.app = app
        self.minimum_size = minimum_size
        self.compresslevel = compresslevel
        self.bypass_paths = tuple(bypass_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._passes_through(scope):
            await self.app(scope, receive, send)
            return
        await _GzipResponder(self.app, self.minimum_size, self.compresslevel)(
            scope, receive, send
        )

    def _passes_through(self, scope: Scope) -> bool:
        if self.bypass_paths and scope["path"].startswith(self.bypass_paths):
            return True
        return not _accepts_gzip(Headers(scope=scope).get("accept-encoding", ""))


class _GzipResponder:
    """One response's state: holds the start message until the body decides."""

    def __init__(self, app: ASGIApp, minimum_size: int, compresslevel: int) -> None:
        self.app = app
        self.minimum_size = minimum_size
        self.compressor = zlib.compressobj(compresslevel, zlib.DEFLATED, 31)
        self.send: Send
        self.start: Message = {}
        self.deciding = False
        self.compressing = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.send = send
        await self.app(scope, receive, self._send_response)

    async def _send_response(self, message: Message) -> None:
        kind = message["type"]
        if kind == "http.response.start":
            self.start = message
            self.deciding = True
            return
        if self.deciding:
            self.deciding = False
            # ``pathsend`` and any other first message are never compressed.
            self.compressing = kind == "http.response.body" and self._should_compress(
                message
            )
            if self.compressing:
                self._encode_into(message)
                self._mark_compressed(message)
            await self.send(self.start)
        elif self.compressing and kind == "http.response.body":
            self._encode_into(message)
        await self.send(message)

    def _should_compress(self, first_body: Message) -> bool:
        headers = Headers(raw=self.start["headers"])
        if "content-encoding" in headers:
            return False
        if "no-transform" in headers.get("cache-control", "").lower():
            return False
        if not _is_compressible_type(headers.get("content-type", "")):
            return False
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
