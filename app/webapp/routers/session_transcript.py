"""Coding tab: a live session's full transcript, paginated (#953), plus
one entry's uncapped text (#985).

``GET /api/claude-code/sessions/{sid}/transcript`` returns one page of typed
entries (``user`` / ``assistant`` / ``tool_call`` / ``tool_result`` /
``thinking`` / ``system``) from the session's *native* history, newest-last,
plus a byte-offset cursor for the next older page. Source resolution is the
same as the Board drawer's ``/exchange`` (#301): the history file the
Board's claim walk assigns to this session-host id — Claude Code's hook
JSONL, or Grok Build's ``updates.jsonl`` (#1012), both carried on the state
row; a Pi session JSONL, named by the claimed row's own *key* (#1013);
else, for a session whose harness writes the launcher nothing at all, a
file correlated from the filesystem — a Codex rollout by cwd + launch
time, an Antigravity conversation by its newest-per-folder cache (#1014),
a Copilot event log by cwd + launch time against its own per-session
``workspace.yaml`` (#1015).
Claude has a filesystem fallback too (#1023): the hook deletes its row on
``/resume`` and ``/clear`` and only the next prompt writes it back, so a
live session can be rowless for as long as the user is reading rather than
typing — the newest conversation in that cwd's own project folder covers
it, refused when a second live Claude session shares the folder or when
nothing was written there since this session started.
A session no source can name answers ``no_transcript`` rather than
guessing a neighbour's file, so a harness that keeps one session folder per
working directory (Grok does, including for directories that no longer
exist) can never show another session's text. None of them read the
launcher's PTY capture, so detached rows are served the same way (#966).
Terminal-grade content, so it sits behind the Tailscale + passkey gate like
``/exchange`` — and so does its sibling below (``middleware.py``'s
``_TERMINAL_GUARD_RULES`` needs its own row per path shape; ``/transcript``'s
rule matches on a ``.../transcript`` suffix, which a ``/transcript/entry``
request does not, so it is a separate row, not covered for free).

The same path also reads *forwards* (#1050): ``?after=<offset>&size=<n>``
returns only what the file has grown by since that offset, which is what the
Chat pane's live refresh ticks on, and answers from one ``stat`` when
``size`` says the file has not grown at all. One path rather than a second
route on purpose — a new path shape needs its own ``_TERMINAL_GUARD_RULES``
row, and this is the same resource, the same caller and the same gate, read
from the other end of the file.

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
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, Request

from src import board
from src.board_exchange import (
    _find_codex_transcript,
    find_antigravity_transcript,
    find_copilot_transcript,
    find_pi_transcript,
    resolve_claude_transcript,
)
from src.session_transcript import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    entry_full_text,
    transcript_page,
    transcript_tail,
)
from src.webapp_config import WebappConfig

from app.webapp.routers.board_spawn import _safe_list_sessions

logger = logging.getLogger(__name__)

router = APIRouter()

# Agents whose native history this reader understands. The values are the
# line grammars in `src.session_transcript.FLAVORS`; the client's own
# availability list (`session-transcript.js`'s `TRANSCRIPT_AGENTS`) mirrors
# the keys, so Chat mode is offered for exactly these agents.
_FLAVOR_BY_AGENT = {
    "claude": "claude", "codex": "codex", "grok": "grok", "pi": "pi",
    "antigravity": "antigravity", "copilot": "copilot",
}


def _unavailable(sid: str, reason: str) -> Dict[str, Any]:
    return {
        "available": False, "source": None, "reason": reason,
        "entries": [], "next_cursor": None, "session_id": sid,
    }


def _resolve_path(
    session: Dict[str, Any],
    row: Optional[Dict[str, Any]],
    state_sid: Optional[str],
    flavor: str,
    live: List[Dict[str, Any]],
) -> Tuple[Optional[Path], str]:
    """The history file for this session (None when none is known), and how.

    The second element is ``""`` for every flavour but Claude, whose
    filesystem fallback says which source answered or which guard refused
    (#1155), for the ``no_transcript`` log line.

    Six shapes, in decreasing directness: the row carries the path
    (Claude, Grok); the row's own *key* is the harness's session id and
    names the file (Pi, #1013); or nothing on the row helps at all and the
    file has to be correlated from the filesystem — by cwd + launch time
    (Codex), by the harness's own newest-conversation-per-folder cache,
    which needs ``live`` to refuse a folder hosting two sessions at once
    (Antigravity, #1014), by cwd + launch time against the harness's own
    per-session ``workspace.yaml`` sidecar (Copilot, #1015), or by newest
    conversation in the cwd's own project folder (Claude, #1023).

    Claude is the one flavour with two: the row is exact and stays first,
    and the filesystem fallback covers the window where the hook has
    deleted the row out from under a still-live session — ``SessionEnd``
    fires on ``/resume`` and ``/clear``, not just on exit, and only the
    next prompt writes the row back. A ``--resume <id>`` launch adds a third,
    for when the fallback's guards refuse: the id names the file (#1155).
    """
    if flavor == "codex":
        return _find_codex_transcript(session), ""
    if flavor == "pi":
        return find_pi_transcript(str(state_sid or "")), ""
    if flavor == "antigravity":
        return find_antigravity_transcript(session, live), ""
    if flavor == "copilot":
        return find_copilot_transcript(session), ""
    raw = (row or {}).get("transcript_path")
    if raw:
        return Path(str(raw)), "row"
    if flavor != "claude":
        return None, ""
    return resolve_claude_transcript(session, live)


async def _resolve_source(
    sid: str, cfg: WebappConfig
) -> Tuple[Optional[str], Optional[str], Optional[Path], str, str]:
    """Session lookup + history-path resolution, shared by both routes.

    Returns ``(reason, flavor, path, agent, why)`` — ``reason`` is set (and
    ``flavor``/``path`` are ``None``) exactly when the caller must respond
    ``_unavailable(sid, reason)`` instead of reading the source. ``why`` is
    how the path was resolved or, for ``no_transcript``, which guard
    refused (``""`` where a flavour has nothing to add).
    """
    live, state = await asyncio.gather(
        asyncio.to_thread(_safe_list_sessions, cfg.session_host_port),
        asyncio.to_thread(board.read_sessions_state, Path(cfg.sessions_state_file)),
    )
    session = next(
        (item for item in live if str(item.get("session_id")) == str(sid)), None
    )
    if session is None:
        return "session_not_found", None, None, "", ""
    agent = str(session.get("agent") or "claude").lower()
    # A detached row resolves exactly like a full-control one (#966): neither
    # source reads the launcher's PTY capture.
    if agent not in _FLAVOR_BY_AGENT:
        return "unsupported_agent", None, None, agent, ""

    flavor = _FLAVOR_BY_AGENT[agent]
    row = board.state_row_for_session(live, state["rows"], sid)
    # Pi's row identifies its history file by its own key rather than by a
    # `transcript_path`, so that flavour alone needs the second lookup —
    # the same in-memory claim walk, resolved to the same row.
    state_sid = (
        board.state_sid_for_session(live, state["rows"], sid) if flavor == "pi" else None
    )
    path, why = await asyncio.to_thread(
        _resolve_path, session, row, state_sid, flavor, live
    )
    if path is None or not path.is_file():
        return "no_transcript", None, None, agent, why
    return None, flavor, path, agent, why


@router.get("/api/claude-code/sessions/{sid}/transcript")
async def session_transcript(
    sid: str,
    request: Request,
    before: Optional[int] = Query(default=None, ge=0),
    after: Optional[int] = Query(default=None, ge=0),
    size: Optional[int] = Query(default=None, ge=0),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> Dict[str, Any]:
    """One page of a live session's transcript (Tailscale + passkey).

    ``before`` is the previous page's ``next_cursor`` (a byte offset; omit
    for the newest page); ``limit`` caps the conversation turns per page.

    ``after`` switches the cursor's direction (#1050): it asks for only what
    has been appended since that offset, which is what the Chat pane's live
    refresh ticks on. ``size`` is the file size the caller last saw, so an
    unchanged file answers from a single ``stat``. The two directions are
    mutually exclusive — one request reads older or newer, never both.

    Deliberately the **same path** as the backwards page rather than a new
    route. A new path would need its own ``_TERMINAL_GUARD_RULES`` row
    (#985's ``/transcript/entry`` is the precedent, and #1035 now fails any
    session-scoped route with no explicit level) — real work for no gain,
    since this serves the same resource to the same caller under the same
    gate, only from the other end of the file.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    if before is not None and after is not None:
        raise HTTPException(
            status_code=400, detail="pass either before or after, not both"
        )
    reason, flavor, path, agent, why = await _resolve_source(sid, cfg)
    if reason is not None:
        if reason != "session_not_found":
            logger.info(
                "ℹ️ transcript %s (%s) unavailable: %s%s",
                sid[:8], agent, reason, f" (refused: {why})" if why else "",
            )
        return _unavailable(sid, reason)
    if after is not None:
        return await _tail_response(sid, agent, flavor, path, after, size)
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
        # Claude's own hook JSONL kept its historical name; every other
        # flavour reports itself, so a third one isn't mislabelled as Codex.
        "source": "native" if flavor == "claude" else flavor,
        "reason": None,
        "session_id": sid,
        **page,
    }


async def _tail_response(
    sid: str,
    agent: str,
    flavor: str,
    path: Path,
    after: int,
    size: Optional[int],
) -> Dict[str, Any]:
    """The forward-cursor half of the route above (#1050).

    Logs nothing on an ordinary tick, on purpose: this runs every few
    seconds for as long as someone is reading a chat, and an info line per
    tick would bury the breadcrumbs that matter under its own traffic. A
    failed read and a reset cursor are rare and diagnostic, so those do log.
    """
    try:
        tail = await asyncio.to_thread(
            transcript_tail, path, after=after, size=size, flavor=flavor
        )
    except OSError as exc:
        logger.warning(
            "⚠️ transcript tail %s (%s) read failed: %s",
            sid[:8], agent, exc.__class__.__name__,
        )
        return _unavailable(sid, "read_failed")
    if tail["reset"]:
        logger.info(
            "ℹ️ transcript tail %s (%s): cursor reset at %d (size %d)",
            sid[:8], agent, after, tail["size"],
        )
    return {
        "available": True,
        "source": "native" if flavor == "claude" else flavor,
        "reason": None,
        "session_id": sid,
        **tail,
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
    reason, flavor, path, agent, _why = await _resolve_source(sid, cfg)
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
