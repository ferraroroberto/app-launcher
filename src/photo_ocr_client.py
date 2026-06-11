"""Thin HTTP client for the sibling photo-ocr single-shot API (issue #171).

The Coding terminal's compose bar can OCR a screenshot: the phone captures
an image and POSTs it to the webapp, which proxies the blob here — to the
photo-ocr's *consumable single-shot endpoint*
(``docs/consuming-the-session-api.md`` in that repo) over loopback. One
call (``POST /api/extract``) creates a session, ingests the image, runs
extraction to completion, and returns clean copy-ready text. The take is
left in photo-ocr's History so it stays recoverable on disk.

The pixel counterpart to ``voice_client`` — same loopback contract, same
auth model: same-host callers bypass photo-ocr's auth gate, and its TLS is
a self-signed loopback cert, so ``verify=False``. These are blocking
``requests`` calls — webapp routes wrap them in ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import requests
import urllib3

logger = logging.getLogger(__name__)

# photo-ocr serves a self-signed loopback cert, so every call sets
# verify=False — silence the one-per-call InsecureRequestWarning that would
# otherwise flood the log (the connection is loopback-only anyway).
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# A vision-hub round-trip on one screenshot is usually a few seconds, but
# allow ample headroom for a cold hub on the first extract after boot.
_TIMEOUT = 120.0


class PhotoOcrError(RuntimeError):
    """Raised when photo-ocr is unreachable or returns an error."""

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
    return f"photo-ocr HTTP {resp.status_code}"


def extract(
    base_url: str,
    files: List[Tuple[str, bytes, str]],
    model: Optional[str] = None,
    prompt_id: Optional[str] = None,
) -> Dict[str, Any]:
    """OCR one or more screenshots via photo-ocr's single-shot
    ``POST /api/extract``.

    ``files`` is a list of ``(filename, content, content_type)`` tuples.
    photo-ocr collates multiple images of one document into a single
    deduplicated text (its whole point). Returns the response —
    ``{"text", "model", "session_id", ...}`` (``text`` may be empty when the
    images hold no readable text). The session is left in photo-ocr's
    History so the take stays recoverable on disk.

    Raises :class:`PhotoOcrError` (carrying an HTTP status) on any transport
    or upstream failure.
    """
    if not files:
        raise PhotoOcrError("no images to OCR", status=400)
    base = base_url.rstrip("/")
    params: Dict[str, Any] = {}
    if model:
        params["model"] = model
    if prompt_id:
        params["prompt_id"] = prompt_id
    # requests sends repeated multipart parts under the same field name when
    # given a list of (field, filespec) tuples — matches photo-ocr's
    # ``files: List[UploadFile]`` contract.
    multipart = [
        ("files", (name, content, ctype)) for (name, content, ctype) in files
    ]
    try:
        resp = requests.post(
            f"{base}/api/extract",
            params=params or None,
            files=multipart,
            timeout=_TIMEOUT,
            verify=False,
        )
    except requests.RequestException as exc:
        raise PhotoOcrError(
            f"photo-ocr unreachable at {base} ({exc})", status=503
        ) from exc
    if resp.status_code >= 400:
        raise PhotoOcrError(_detail(resp), status=resp.status_code)
    try:
        return dict(resp.json())
    except ValueError as exc:
        raise PhotoOcrError(
            f"photo-ocr extract returned non-JSON ({exc})"
        ) from exc
