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

from src import _loopback_http

logger = logging.getLogger(__name__)

# One ``/api/extract`` call is bounded by photo-ocr's own run budget, not by a
# single hub round-trip: it reads photos in parallel with a retry and answers
# within ``extract_run_budget_s`` (900 s by default, photo-ocr#166). Wait a
# little longer than that so we never give up on a run photo-ocr is still
# entitled to finish — a client-side timeout would discard text it then returns
# (#1420). A usual 1-2 screenshot attach still answers in 10-15 s.
_TIMEOUT = 960.0

# photo-ocr serves a self-signed loopback cert, so every call sets verify=False
# (the per-call InsecureRequestWarning is suppressed once in _loopback_http).
_VERIFY = False


class PhotoOcrError(_loopback_http.LoopbackError):
    """Raised when photo-ocr is unreachable or returns an error."""


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
    ``{"text", "model", "session_id", "missing_photos", ...}`` (``text`` may be
    empty when the images hold no readable text). A partly read take is a
    *success*: ``missing_photos`` names the unread files and ``text`` carries a
    ``[missing: photo N ...]`` line where each belongs. The session is left in
    photo-ocr's History so the take stays recoverable on disk.

    Raises :class:`PhotoOcrError` (carrying an HTTP status) on any transport
    or upstream failure, keeping three states apart: 503 photo-ocr unreachable,
    504 connected but no answer within ``_TIMEOUT`` (still running), and
    photo-ocr's own status (502: no photo could be read) with its detail.
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
    result = _loopback_http.request(
        "POST",
        f"{base}/api/extract",
        error=PhotoOcrError,
        service="photo-ocr",
        timeout=_TIMEOUT,
        verify=_VERIFY,
        allow_empty=False,
        read_timeout_status=504,
        params=params or None,
        files=multipart,
    )
    missing = result.get("missing_photos") or []
    result["missing_photos"] = missing
    if missing:
        logger.warning(
            "⚠️ photo-ocr read %d of %d photos; unread: %s",
            len(files) - len(missing), len(files), ", ".join(map(str, missing)),
        )
    return result
