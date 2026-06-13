"""Thin HTTP client for the sibling voice-transcriber session API (issue #165).

The Coding terminal's compose bar can dictate a prompt: the phone records
audio and POSTs it to the webapp, which proxies the blob here — to the
voice-transcriber's *consumable session API*
(``docs/consuming-the-session-api.md`` in that repo) over loopback. We use
the single-shot path (create a session, then upload the whole blob), which
already persists the audio to disk before transcoding, so the take is safe
on the PC the same way a recording made in the voice-transcriber UI is.

Same-host contract: loopback callers bypass the voice-transcriber's auth
gate, and its TLS is a self-signed loopback cert, so ``verify=False``. These
are blocking ``requests`` calls — webapp routes wrap them in
``asyncio.to_thread`` (mirroring ``session_client``).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import requests

from src import _loopback_http

logger = logging.getLogger(__name__)

# Whisper on a short dictation is quick, but allow ample headroom for a
# cold whisper-server plus ffmpeg transcode on the first take after boot.
_TIMEOUT = 60.0
_CREATE_TIMEOUT = 15.0

# The voice-transcriber serves a self-signed loopback cert, so every call sets
# verify=False (the per-call InsecureRequestWarning is suppressed once in
# _loopback_http).
_VERIFY = False


class VoiceTranscriberError(_loopback_http.LoopbackError):
    """Raised when the voice-transcriber is unreachable or returns an error."""


def _request(method: str, url: str, *, timeout: float, allow_empty: bool, **kwargs: Any) -> Any:
    return _loopback_http.request(
        method,
        url,
        error=VoiceTranscriberError,
        service="voice-transcriber",
        timeout=timeout,
        verify=_VERIFY,
        allow_empty=allow_empty,
        **kwargs,
    )


def transcribe(
    base_url: str,
    filename: str,
    content: bytes,
    content_type: str,
    language: Optional[str] = None,
) -> Dict[str, Any]:
    """Transcribe one recorded audio blob via the single-shot session API.

    Creates a session, uploads the whole blob, and returns the upload
    response — ``{"transcript", "language", ...}`` (with ``silent: true``
    and an empty transcript on near-silent audio). The session is left in
    the voice-transcriber's History so the take stays recoverable on disk.

    Raises :class:`VoiceTranscriberError` (carrying an HTTP status) on any
    transport or upstream failure.
    """
    base = base_url.rstrip("/")
    create_body: Dict[str, Any] = {}
    if language:
        create_body["language"] = language
    created = _request(
        "POST", f"{base}/api/sessions", timeout=_CREATE_TIMEOUT, allow_empty=True,
        json=create_body,
    )
    sid = created.get("session_id") if isinstance(created, dict) else None
    if not sid:
        raise VoiceTranscriberError("voice-transcriber create returned no session_id")

    params = {"language": language} if language else None
    return _request(
        "POST", f"{base}/api/sessions/{sid}/upload", timeout=_TIMEOUT, allow_empty=False,
        params=params,
        files={"file": (filename, content, content_type)},
    )


# --- streamed "never-lose-it" path (issue #168) -------------------------
# The single-shot path above gives no feedback until /upload returns. For
# live rolling partials the compose bar drives the chunked flow instead:
# create → repeated chunk (1 s cadence) → SSE partials (proxied async in
# the router) → finish. These helpers wrap the non-SSE legs.


def create_session(base_url: str, language: Optional[str] = None) -> Dict[str, Any]:
    """Create a streamed transcription session; return its JSON body."""
    base = base_url.rstrip("/")
    body: Dict[str, Any] = {}
    if language:
        body["language"] = language
    return _request(
        "POST", f"{base}/api/sessions", timeout=_CREATE_TIMEOUT, allow_empty=False,
        json=body,
    )


def send_chunk(
    base_url: str, session_id: str, content: bytes, content_type: str
) -> Dict[str, Any]:
    """Append one raw audio chunk to a streamed session (body is raw bytes)."""
    base = base_url.rstrip("/")
    return _request(
        "POST", f"{base}/api/sessions/{session_id}/chunk", timeout=_TIMEOUT,
        allow_empty=True,
        data=content,
        headers={"Content-Type": content_type},
    )


def finish(
    base_url: str, session_id: str, language: Optional[str] = None
) -> Dict[str, Any]:
    """Close a streamed session and return the canonical transcript."""
    base = base_url.rstrip("/")
    params = {"language": language} if language else None
    return _request(
        "POST", f"{base}/api/sessions/{session_id}/finish", timeout=_TIMEOUT,
        allow_empty=False,
        params=params,
    )


def events_url(base_url: str, session_id: str) -> str:
    """Upstream SSE URL for a session's rolling-partial stream."""
    return f"{base_url.rstrip('/')}/api/sessions/{session_id}/events"
