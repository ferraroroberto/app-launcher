"""Gzip for the text a phone downloads on launch: HTML, JS, CSS, JSON, SVG.

Starlette's ``GZipMiddleware`` compresses every response over ``minimum_size``
except ``text/event-stream``. That is wrong here in two ways: stored images,
PDFs and outbox files are already compressed (gzip spends CPU for no gain), and
the TTS proxy streams raw PCM (``audio/L16``) chunk by chunk, where gzip holds
bytes back in its compressor and the audio would stall. So this middleware
compresses an allowlist of text types and passes everything else through
untouched, on the same responders Starlette uses.

Register it before ``BearerTokenMiddleware`` so it sits inside the auth gate: a
``BaseHTTPMiddleware`` re-streams the body in chunks, and a gzip layer outside
it would ignore ``minimum_size`` and compress a bodyless 304.
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.middleware.gzip import GZipMiddleware, GZipResponder, IdentityResponder
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Below this a response costs more in gzip framing and CPU than it saves.
MINIMUM_SIZE = 1000

_COMPRESSIBLE_TYPES = (
    "text/",
    "application/json",
    "application/javascript",
    "application/manifest+json",
    "image/svg+xml",
)


def is_compressible(content_type: str) -> bool:
    """Whether a response of this ``Content-Type`` is text worth gzipping."""
    lowered = content_type.lower()
    if lowered.startswith("text/event-stream"):
        return False
    return lowered.startswith(_COMPRESSIBLE_TYPES)


class _TextOnly:
    """Responder mixin: leave a response alone unless its type is compressible."""

    async def send_with_compression(self, message: Message) -> None:
        await super().send_with_compression(message)  # type: ignore[misc]
        if message["type"] == "http.response.start":
            content_type = Headers(raw=message["headers"]).get("content-type", "")
            self.content_type_is_excluded = not is_compressible(content_type)  # type: ignore[attr-defined]


class _TextOnlyGZip(_TextOnly, GZipResponder):
    pass


class _TextOnlyIdentity(_TextOnly, IdentityResponder):
    pass


class TextGZipMiddleware(GZipMiddleware):
    """``GZipMiddleware`` restricted to text responses."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        responder: ASGIApp
        if "gzip" in Headers(scope=scope).get("Accept-Encoding", ""):
            responder = _TextOnlyGZip(self.app, self.minimum_size, compresslevel=self.compresslevel)
        else:
            responder = _TextOnlyIdentity(self.app, self.minimum_size)
        await responder(scope, receive, send)
