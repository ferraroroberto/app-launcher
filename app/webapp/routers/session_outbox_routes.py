"""Coding tab: files an agent sends to the owner's chat view (#1530).

``POST /api/claude-code/sessions/{sid}/outbox`` — ``{"path", "caption"?}``.
**Loopback-only**: the caller is an agent running on this PC (a companion
``send_file`` script, any harness). A request from anywhere else is refused
outright, passkey or not — sending a file is the agent's act, never the
phone's. The file is checked and copied by :mod:`src.session_outbox`; each
refusal comes back with its own ``reason`` and status.

``GET /api/claude-code/sessions/{sid}/outbox`` lists a session's entries and
``GET .../outbox/{entry_id}`` serves one entry's bytes. Both are
terminal-grade content behind the Tailscale + passkey gate
(``middleware._TERMINAL_GUARD_RULES``), like ``/transcript/image``.

The Chat pane does not poll the list: the transcript page and live tick
carry the outbox alongside the entries (``session_transcript.py``), so a
chat being read costs no extra request.

Bodies, paths and captions are never logged; a refusal logs its reason.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

from src import session_outbox
from src.webapp_config import WebappConfig

from app.webapp.middleware import is_pc_itself
from app.webapp.routers.board_spawn import SESSION_HOST_UNREACHABLE, _read_live_sessions

logger = logging.getLogger(__name__)

router = APIRouter()

# A refusal's HTTP status: each condition its own, so a caller that only
# reads the status still tells them apart where it matters.
_REFUSAL_STATUS = {
    "bad_request": 400,
    "not_found": 404,
    "not_a_file": 422,
    "out_of_root": 403,
    "too_large": 413,
    "type_not_allowed": 415,
    "content_mismatch": 415,
}


def refusal_error(exc: session_outbox.OutboxRefusal) -> HTTPException:
    return HTTPException(
        status_code=_REFUSAL_STATUS.get(exc.reason, 400),
        detail={"reason": exc.reason, "message": exc.message},
    )


def file_headers(name: str, media: str) -> Dict[str, str]:
    """Headers for serving a sent file. Nothing stores it (a private
    capture), nothing sniffs it into another type, the name is a quoted
    ``filename*`` (never raw header text), and anything but a PDF — which
    the browser's own viewer must be free to render — is sandboxed."""
    headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": "inline; filename*=UTF-8''" + quote(name, safe=""),
    }
    if media != session_outbox.PDF_MEDIA:
        headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return headers


def outbox_fields(sid: str, seen_rev: Optional[int]) -> Dict[str, Any]:
    """What a transcript page or live tick carries about the outbox:
    ``outbox_rev`` always, and the full ``outbox`` list only when it differs
    from ``seen_rev`` (the rev the client already holds)."""
    found = session_outbox.listing(sid)
    out: Dict[str, Any] = {"outbox_rev": found["rev"]}
    if seen_rev is None or seen_rev != found["rev"]:
        out["outbox"] = found["entries"]
    return out


@router.post("/api/claude-code/sessions/{sid}/outbox")
async def post_outbox(sid: str, request: Request) -> Dict[str, Any]:
    """Send a file to this session's chat (loopback only)."""
    client_host = request.client.host if request.client else ""
    if not is_pc_itself(client_host, request.headers):
        logger.info("ℹ️ outbox %s refused: not loopback", sid[:8])
        raise HTTPException(
            status_code=403,
            detail={"reason": "loopback_only", "message": "the outbox takes files from this PC only"},
        )
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("path"), str):
        raise HTTPException(
            status_code=400,
            detail={"reason": "bad_request", "message": 'expected JSON {"path": "...", "caption": "..."}'},
        )
    cfg: WebappConfig = request.app.state.webapp_config
    live, host_error = await asyncio.to_thread(_read_live_sessions, cfg.session_host_port)
    session = next((s for s in live if str(s.get("session_id")) == str(sid)), None)
    if session is None:
        if host_error:
            raise HTTPException(
                status_code=503,
                detail={"reason": SESSION_HOST_UNREACHABLE, "message": "the session-host is not answering"},
            )
        raise HTTPException(
            status_code=404,
            detail={"reason": "session_not_found", "message": "no live session with that id"},
        )
    project_dir = str(session.get("project_dir") or "") or None
    roots = session_outbox.allowed_roots(project_dir, cfg.outbox_scratch_roots)
    try:
        entry = await asyncio.to_thread(
            session_outbox.post, sid, body["path"], body.get("caption"), project_dir, roots,
        )
    except session_outbox.OutboxRefusal as exc:
        logger.info("ℹ️ outbox %s refused: %s", sid[:8], exc.reason)
        raise refusal_error(exc) from None
    return {"ok": True, "session_id": sid, "entry": entry}


@router.get("/api/claude-code/sessions/{sid}/outbox")
async def list_outbox(sid: str) -> Dict[str, Any]:
    """A session's sent files, oldest first (Tailscale + passkey)."""
    found = await asyncio.to_thread(session_outbox.listing, sid)
    return {"session_id": sid, **found}


@router.get("/api/claude-code/sessions/{sid}/outbox/{entry_id}")
async def outbox_file(sid: str, entry_id: str) -> Response:
    """One sent file's bytes (Tailscale + passkey)."""
    found = await asyncio.to_thread(session_outbox.entry_file, sid, entry_id)
    if found is None:
        raise HTTPException(status_code=404, detail={"reason": "entry_not_found", "message": "no such file"})
    stored, entry = found
    media = str(entry.get("media") or "")
    if media not in session_outbox.SERVED_MEDIA:
        raise HTTPException(status_code=404, detail={"reason": "entry_not_found", "message": "no such file"})
    return FileResponse(
        Path(stored), media_type=media,
        headers=file_headers(str(entry.get("name") or "file"), media),
    )
