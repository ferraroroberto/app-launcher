"""Coding tab: a live session's full transcript, paginated (#953).

``GET /api/claude-code/sessions/{sid}/transcript`` returns one page of typed
entries (``user`` / ``assistant`` / ``tool_call`` / ``tool_result`` /
``thinking`` / ``system``) from the session's *native* history, newest-last,
plus a byte-offset cursor for the next older page. Source resolution is the
same as the Board drawer's ``/exchange`` (#301): the Claude Code hook JSONL
the Board's claim walk assigns to this session-host id, else — for a Codex
session — the rollout correlated by cwd + launch time. Neither reads the
launcher's PTY capture, so detached rows are served the same way (#966).
Terminal-grade content, so it sits behind the Tailscale + passkey gate like
``/exchange``.

Unavailable sources are told apart on purpose (``reason``): a session the
host doesn't know, an agent with no structured history, a history file that
isn't there, and a file that *is* there but couldn't be read. Bodies are
never logged.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Query, Request

from src import board
from src.board_exchange import _find_codex_transcript
from src.session_transcript import DEFAULT_LIMIT, MAX_LIMIT, transcript_page
from src.webapp_config import WebappConfig

from app.webapp.routers.board_spawn import _safe_list_sessions

logger = logging.getLogger(__name__)

router = APIRouter()

# Agents whose native history this reader understands.
_FLAVOR_BY_AGENT = {"claude": "claude", "codex": "codex"}


def _unavailable(sid: str, reason: str) -> Dict[str, Any]:
    return {
        "available": False, "source": None, "reason": reason,
        "entries": [], "next_cursor": None, "session_id": sid,
    }


def _resolve_path(
    session: Dict[str, Any], row: Optional[Dict[str, Any]], flavor: str
) -> Optional[Path]:
    """The history file for this session, or None when none is known."""
    if flavor == "codex":
        return _find_codex_transcript(session)
    raw = (row or {}).get("transcript_path")
    return Path(str(raw)) if raw else None


@router.get("/api/claude-code/sessions/{sid}/transcript")
async def session_transcript(
    sid: str,
    request: Request,
    before: Optional[int] = Query(default=None, ge=0),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> Dict[str, Any]:
    """One page of a live session's transcript (Tailscale + passkey).

    ``before`` is the previous page's ``next_cursor`` (a byte offset; omit
    for the newest page); ``limit`` caps the conversation turns per page.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    live, state = await asyncio.gather(
        asyncio.to_thread(_safe_list_sessions, cfg.session_host_port),
        asyncio.to_thread(board.read_sessions_state, Path(cfg.sessions_state_file)),
    )
    session = next(
        (item for item in live if str(item.get("session_id")) == str(sid)), None
    )
    if session is None:
        return _unavailable(sid, "session_not_found")
    agent = str(session.get("agent") or "claude").lower()
    # A detached row resolves exactly like a full-control one (#966): neither
    # source reads the launcher's PTY capture.
    if agent not in _FLAVOR_BY_AGENT:
        logger.info("ℹ️ transcript %s (%s) unavailable: unsupported_agent", sid[:8], agent)
        return _unavailable(sid, "unsupported_agent")

    flavor = _FLAVOR_BY_AGENT[agent]
    row = board.state_row_for_session(live, state["rows"], sid)
    path = await asyncio.to_thread(_resolve_path, session, row, flavor)
    if path is None or not path.is_file():
        logger.info("ℹ️ transcript %s (%s) unavailable: no_transcript", sid[:8], agent)
        return _unavailable(sid, "no_transcript")
    try:
        page = await asyncio.to_thread(
            transcript_page, path, before=before, limit=limit, flavor=flavor
        )
    except OSError as exc:
        logger.warning(
            "⚠️ transcript %s (%s) read failed: %s", sid[:8], agent, exc.__class__.__name__
        )
        return _unavailable(sid, "read_failed")
    logger.info(
        "ℹ️ transcript %s (%s) page: %d entries, source=%s, more=%s",
        sid[:8], agent, len(page["entries"]), flavor, page["next_cursor"] is not None,
    )
    return {
        "available": True,
        "source": "native" if flavor == "claude" else "codex",
        "reason": None,
        "session_id": sid,
        **page,
    }
