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
import urllib3

logger = logging.getLogger(__name__)

# The voice-transcriber serves a self-signed loopback cert, so every call
# sets verify=False — silence the one-per-call InsecureRequestWarning that
# would otherwise flood the log (the connection is loopback-only anyway).
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Whisper on a short dictation is quick, but allow ample headroom for a
# cold whisper-server plus ffmpeg transcode on the first take after boot.
_TIMEOUT = 60.0
_CREATE_TIMEOUT = 15.0


class VoiceTranscriberError(RuntimeError):
    """Raised when the voice-transcriber is unreachable or returns an error."""

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


def _detail(resp: requests.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("detail"):
            return str(body["detail"])
    except ValueError:
        pass
    return f"voice-transcriber HTTP {resp.status_code}"


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
    try:
        resp = requests.post(
            f"{base}/api/sessions",
            json=create_body,
            timeout=_CREATE_TIMEOUT,
            verify=False,
        )
    except requests.RequestException as exc:
        raise VoiceTranscriberError(
            f"voice-transcriber unreachable at {base} ({exc})", status=503
        ) from exc
    if resp.status_code >= 400:
        raise VoiceTranscriberError(_detail(resp), status=resp.status_code)
    try:
        sid = str(resp.json()["session_id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise VoiceTranscriberError(
            f"voice-transcriber create returned no session_id ({exc})"
        ) from exc

    params = {"language": language} if language else None
    try:
        up = requests.post(
            f"{base}/api/sessions/{sid}/upload",
            params=params,
            files={"file": (filename, content, content_type)},
            timeout=_TIMEOUT,
            verify=False,
        )
    except requests.RequestException as exc:
        raise VoiceTranscriberError(
            f"voice-transcriber upload failed ({exc})", status=503
        ) from exc
    if up.status_code >= 400:
        raise VoiceTranscriberError(_detail(up), status=up.status_code)
    try:
        return dict(up.json())
    except ValueError as exc:
        raise VoiceTranscriberError(
            f"voice-transcriber upload returned non-JSON ({exc})"
        ) from exc
