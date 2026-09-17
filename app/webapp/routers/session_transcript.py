"""Coding tab: a live session's full transcript, paginated (#953), plus
one entry's uncapped text (#985).

``GET /api/claude-code/sessions/{sid}/transcript`` returns one page of typed
entries (``user`` / ``assistant`` / ``tool_call`` / ``tool_result`` /
``thinking`` / ``system``) from the session's *native* history, newest-last,
plus a byte-offset cursor for the next older page. Source resolution is the
same as the Board drawer's ``/exchange`` (#301): the Claude Code hook JSONL
the Board's claim walk assigns to this session-host id, else — for a Codex
session — the rollout correlated by cwd + launch time. Neither reads the
launcher's PTY capture, so detached rows are served the same way (#966).
Terminal-grade content, so it sits behind the Tailscale + passkey gate like
``/exchange`` — and so does its sibling below (``middleware.py``'s
``_TERMINAL_GUARD_RULES`` needs its own row per path shape; ``/transcript``'s
rule matches on a ``.../transcript`` suffix, which a ``/transcript/entry``
request does not, so it is a separate row, not covered for free).

``GET /api/claude-code/sessions/{sid}/transcript/entry?offset=<n>`` re-reads
one ``user``/``assistant`` turn from the same source, uncapped — the Chat
pane's copy button (#985) calls it only when the page's own copy of that
entry came back ``truncated: true``. Same source resolution, same gate.

Unavailable sources are told apart on purpose (``reason``): a session the
host doesn't know, an agent with no structured history, a history file that
isn't there, and a file that *is* there but couldn't be read. Bodies are
never logged.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Query, Request

from src import board
from src.board_exchange import _find_codex_transcript
from src.session_transcript import DEFAULT_LIMIT, MAX_LIMIT, entry_full_text, transcript_page
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


async def _resolve_source(
    sid: str, cfg: WebappConfig
) -> Tuple[Optional[str], Optional[str], Optional[Path], str]:
    """Session lookup + history-path resolution, shared by both routes.

    Returns ``(reason, flavor, path, agent)`` — ``reason`` is set (and
    ``flavor``/``path`` are ``None``) exactly when the caller must respond
    ``_unavailable(sid, reason)`` instead of reading the source.
    """
    live, state = await asyncio.gather(
        asyncio.to_thread(_safe_list_sessions, cfg.session_host_port),
        asyncio.to_thread(board.read_sessions_state, Path(cfg.sessions_state_file)),
    )
    session = next(
        (item for item in live if str(item.get("session_id")) == str(sid)), None
    )
    if session is None:
        return "session_not_found", None, None, ""
    agent = str(session.get("agent") or "claude").lower()
    # A detached row resolves exactly like a full-control one (#966): neither
    # source reads the launcher's PTY capture.
    if agent not in _FLAVOR_BY_AGENT:
        return "unsupported_agent", None, None, agent

    flavor = _FLAVOR_BY_AGENT[agent]
    row = board.state_row_for_session(live, state["rows"], sid)
    path = await asyncio.to_thread(_resolve_path, session, row, flavor)
    if path is None or not path.is_file():
        return "no_transcript", None, None, agent
    return None, flavor, path, agent


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
    reason, flavor, path, agent = await _resolve_source(sid, cfg)
    if reason is not None:
        if reason != "session_not_found":
            logger.info("ℹ️ transcript %s (%s) unavailable: %s", sid[:8], agent, reason)
        return _unavailable(sid, reason)
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


@router.get("/api/claude-code/sessions/{sid}/transcript/entry")
async def session_transcript_entry(
    sid: str,
    request: Request,
    offset: int = Query(ge=0),
) -> Dict[str, Any]:
    """One ``user``/``assistant`` turn, uncapped (Tailscale + passkey).

    ``offset`` is the byte offset a page entry carried when the page served
    it ``truncated: true``. ``reason: "entry_not_found"`` covers both a bad
    offset and the ordinary race of the source file moving on (rotated,
    compacted) between the page load and this call — nothing to distinguish
    them by, and the client's fallback (keep the capped text) is the same
    either way.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    reason, flavor, path, agent = await _resolve_source(sid, cfg)
    if reason is not None:
        return _unavailable(sid, reason)
    try:
        entry = await asyncio.to_thread(entry_full_text, path, offset, flavor)
    except OSError as exc:
        logger.warning(
            "⚠️ transcript entry %s (%s) read failed: %s", sid[:8], agent, exc.__class__.__name__
        )
        return _unavailable(sid, "read_failed")
    if entry is None:
        logger.info("ℹ️ transcript entry %s (%s) unavailable: entry_not_found", sid[:8], agent)
        return _unavailable(sid, "entry_not_found")
    logger.info(
        "ℹ️ transcript entry %s (%s): %d chars, truncated=%s",
        sid[:8], agent, len(entry["text"]), entry["truncated"],
    )
    return {
        "available": True, "reason": None, "session_id": sid,
        "text": entry["text"], "truncated": entry["truncated"],
    }
