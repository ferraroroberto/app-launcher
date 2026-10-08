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
nothing was written there since this session started. Where it refuses, a
``--resume <id>`` launch (#1155) or a Remote Control link (#1393: its bridge
id names the session's own Claude process in Claude Code's per-pid registry,
which is how a Telegram channel session with no hook row reads its chat in a
folder shared by its siblings) still names the file exactly.
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

The terminal-screen routes (answering a pending question, the plan picker,
resuming a conversation, context use) live in ``session_screen.py``.

Unavailable sources are told apart on purpose (``reason``): a session the
host doesn't know, an agent with no structured history, a history file that
isn't there, and a file that *is* there but couldn't be read. Bodies are
never logged.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, Request, Response

from src import board
from src.board_transcript import _live_title_is_busy, _pty_output_is_fresh
from src.transcript_locate import (
    find_antigravity_transcript,
    find_codex_transcript,
    find_copilot_transcript,
    find_pi_transcript,
    resolve_claude_transcript,
)
from src.session_transcript import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    entry_diff,
    entry_full_text,
    transcript_activity,
    transcript_image,
    transcript_page,
    transcript_tail,
)
from src.session_changes import changed_files, file_steps
from src.webapp_config import WebappConfig

from app.webapp.routers._helpers import attach_provider_web_urls
from app.webapp.routers.board_spawn import SESSION_HOST_UNREACHABLE, _read_live_sessions

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
    for when the fallback's guards refuse: the id names the file (#1155). A
    Remote Control link adds a fourth: its bridge id names the file through
    Claude Code's per-pid registry (#1393).
    """
    if flavor == "codex":
        return find_codex_transcript(session), ""
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
    path, why = resolve_claude_transcript(session, live)
    if path is not None or session.get("web_url"):
        return path, why
    # Nothing cheaper named the file. The session-host's row carries no Remote
    # Control link (the Coding tab and the Board attach it from the PTY
    # capture), so read it the same way and let its bridge id answer (#1399):
    # only on a refusal, since the capture read is the dearer step. On a
    # copy, so the caller's row stays as the host reported it.
    linked = dict(session)
    attach_provider_web_urls([linked])
    if not linked.get("web_url"):
        return path, why
    return resolve_claude_transcript(linked, live)


async def _resolve_source(
    sid: str, cfg: WebappConfig
) -> Tuple[Optional[str], Optional[str], Optional[Path], str, str, Optional[Dict[str, Any]]]:
    """Session lookup + history-path resolution, shared by every route here.

    Returns ``(reason, flavor, path, agent, why, session)`` — ``reason`` is
    set (and ``flavor``/``path`` are ``None``) exactly when the caller must
    respond ``_unavailable(sid, reason)`` instead of reading the source.
    ``why`` is how the path was resolved or, for ``no_transcript``, which
    guard refused (``""`` where a flavour has nothing to add). ``session``
    is the session-host's row, ``None`` only for ``session_not_found`` and
    ``session_host_unreachable`` (the list could not be read, #1308: not the
    same fact as "the session is not in it").
    """
    (live, host_error), state = await asyncio.gather(
        asyncio.to_thread(_read_live_sessions, cfg.session_host_port),
        asyncio.to_thread(board.read_sessions_state, Path(cfg.sessions_state_file)),
    )
    session = next(
        (item for item in live if str(item.get("session_id")) == str(sid)), None
    )
    if session is None:
        reason = SESSION_HOST_UNREACHABLE if host_error else "session_not_found"
        return reason, None, None, "", "", None
    agent = str(session.get("agent") or "claude").lower()
    # A detached row resolves exactly like a full-control one (#966): neither
    # source reads the launcher's PTY capture.
    if agent not in _FLAVOR_BY_AGENT:
        return "unsupported_agent", None, None, agent, "", session

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
        return "no_transcript", None, None, agent, why, session
    return None, flavor, path, agent, why, session


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
    reason, flavor, path, agent, why, session = await _resolve_source(sid, cfg)
    if reason is not None:
        # Neither of these is a source problem, and the live tick asks every
        # few seconds: the unreachable one is already latched by the list read.
        if reason not in ("session_not_found", SESSION_HOST_UNREACHABLE):
            logger.info(
                "ℹ️ transcript %s (%s) unavailable: %s%s",
                sid[:8], agent, reason, f" (refused: {why})" if why else "",
            )
        return _unavailable(sid, reason)
    if after is not None:
        return await _tail_response(sid, agent, flavor, path, after, size, session)
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
    body = {
        "available": True,
        # Claude's own hook JSONL kept its historical name; every other
        # flavour reports itself, so a third one isn't mislabelled as Codex.
        "source": "native" if flavor == "claude" else flavor,
        "reason": None,
        "session_id": sid,
        **page,
    }
    if before is None:
        # The newest page is the chat's first read: its activity line starts here.
        body["activity"] = await _activity(session, flavor, path)
    return body


async def _tail_response(
    sid: str,
    agent: str,
    flavor: str,
    path: Path,
    after: int,
    size: Optional[int],
    session: Optional[Dict[str, Any]],
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
        "activity": await _activity(session, flavor, path),
    }


async def _activity(
    session: Optional[Dict[str, Any]], flavor: str, path: Path
) -> Optional[Dict[str, Any]]:
    """The chat strip's one-line summary of the turn in progress (#1387):
    ``since`` / ``actions`` / ``last`` / ``working``, or ``None`` when the
    transcript cannot say. Rides the page and tail responses — no route or
    poll of its own — and costs a ``stat`` while the file is unchanged.

    Claude's busy signal is its animated title glyph on a PTY that is still
    producing output, the same evidence the Board's status uses; the other
    harnesses have none, so theirs is read off the transcript alone.
    """
    busy = (
        flavor == "claude"
        and session is not None
        and _live_title_is_busy(session.get("live_title"))
        and _pty_output_is_fresh(session.get("last_output_at"), datetime.now(timezone.utc))
    )
    return await asyncio.to_thread(transcript_activity, path, flavor=flavor, busy=busy)


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
    reason, flavor, path, agent, _why, _session = await _resolve_source(sid, cfg)
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


@router.get("/api/claude-code/sessions/{sid}/transcript/diff")
async def session_transcript_diff(
    sid: str,
    request: Request,
    offset: int = Query(ge=0),
    n: int = Query(default=0, ge=0, le=999),
) -> Dict[str, Any]:
    """One edit step's whole diff (Tailscale + passkey, #1349).

    ``offset`` and ``n`` are the ``diff.n`` ref a page's tool call carried
    when its inline diff came ``truncated``: the line the call was built
    from, and its index among that line's calls. ``reason: "diff_not_found"``
    covers a bad ref and a file that moved on alike — the client keeps the
    inline diff it already has either way.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    reason, flavor, path, agent, _why, _session = await _resolve_source(sid, cfg)
    if reason is not None:
        return _unavailable(sid, reason)
    try:
        found = await asyncio.to_thread(entry_diff, path, offset, n, flavor)
    except OSError as exc:
        logger.warning(
            "⚠️ transcript diff %s (%s) read failed: %s", sid[:8], agent, exc.__class__.__name__
        )
        return _unavailable(sid, "read_failed")
    if found is None:
        logger.info("ℹ️ transcript diff %s (%s) unavailable at %d/%d", sid[:8], agent, offset, n)
        return _unavailable(sid, "diff_not_found")
    diff = found["diff"]
    logger.info(
        "ℹ️ transcript diff %s (%s): %d hunks, numbered=%s, truncated=%s",
        sid[:8], agent, len(diff["hunks"]), diff["numbered"], diff["truncated"],
    )
    return {
        "available": True, "reason": None, "session_id": sid,
        "path": found["path"], "diff": diff,
    }


@router.get("/api/claude-code/sessions/{sid}/changed-files")
async def session_changed_files(sid: str, request: Request) -> Dict[str, Any]:
    """Every file this session's edits touched (Tailscale + passkey, #1349).

    Folded from the transcript, never ``git diff`` — see
    :mod:`src.session_changes` for why. Every agent with a transcript reader
    (#1356); ``reason: "unsupported_agent"`` for one without, whose ⋮ menu
    hides the item anyway. On demand only, like Show changes: nothing polls
    this.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    reason, flavor, path, agent, _why, session = await _resolve_source(sid, cfg)
    if reason is not None:
        logger.info("ℹ️ changed files %s (%s) unavailable: %s", sid[:8], agent, reason)
        return _unavailable(sid, reason)
    project_dir = str((session or {}).get("project_dir") or "") or None
    try:
        found = await asyncio.to_thread(changed_files, path, project_dir, flavor)
    except OSError as exc:
        logger.warning(
            "⚠️ changed files %s (%s) read failed: %s", sid[:8], agent, exc.__class__.__name__
        )
        return _unavailable(sid, "read_failed")
    logger.info(
        "ℹ️ changed files %s (%s): %d files, +%d -%d, partial=%s, project_exists=%s",
        sid[:8], agent, len(found["files"]), found["counts"]["additions"],
        found["counts"]["deletions"], found["partial"], found["project_exists"],
    )
    return {
        "available": True, "reason": None, "session_id": sid,
        "source": "transcript", **found,
    }


@router.get("/api/claude-code/sessions/{sid}/changed-files/diff")
async def session_changed_file_diff(
    sid: str, request: Request, path: str = Query(min_length=1),
) -> Dict[str, Any]:
    """One changed file's diff: each of this session's edits to it, in
    order (Tailscale + passkey, #1349). ``path`` is a ``key`` the list
    handed out; a file the session never edited is ``file_not_found`` —
    nothing here reads the file system, only the transcript."""
    cfg: WebappConfig = request.app.state.webapp_config
    reason, flavor, source, agent, _why, session = await _resolve_source(sid, cfg)
    if reason is not None:
        return _unavailable(sid, reason)
    project_dir = str((session or {}).get("project_dir") or "") or None
    try:
        found = await asyncio.to_thread(
            file_steps, source, path, flavor=flavor, project_dir=project_dir
        )
    except OSError as exc:
        logger.warning(
            "⚠️ changed file diff %s (%s) read failed: %s", sid[:8], agent, exc.__class__.__name__
        )
        return _unavailable(sid, "read_failed")
    if found is None:
        logger.info("ℹ️ changed file diff %s (%s): file_not_found", sid[:8], agent)
        return _unavailable(sid, "file_not_found")
    logger.info(
        "ℹ️ changed file diff %s (%s): %d steps, truncated=%s",
        sid[:8], agent, len(found["steps"]), found["truncated"],
    )
    return {"available": True, "reason": None, "session_id": sid, **found}


@router.get("/api/claude-code/sessions/{sid}/transcript/image")
async def session_transcript_image(
    sid: str,
    request: Request,
    offset: int = Query(ge=0),
    n: int = Query(ge=0, le=999),
) -> Response:
    """One transcript image's bytes, decoded on demand (Tailscale + passkey, #1265).

    ``offset`` and ``n`` are an entry's ``images``/``result_images`` ref:
    the line the image lives on and its index among that line's images. The
    page never carries image bytes; the thumbnails fetch this, lazily. A
    session's images can be private captures, so nothing may store them:
    ``no-store``, and the type is sniffed from the bytes, never taken from
    the block. 404 covers a bad ref, a file that moved on, and a block that
    isn't an allowlisted image alike: the client's answer (no thumbnail) is
    the same.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    reason, flavor, path, agent, _why, _session = await _resolve_source(sid, cfg)
    if reason == SESSION_HOST_UNREACHABLE:
        raise HTTPException(status_code=503, detail=reason)
    if reason is not None:
        raise HTTPException(status_code=404, detail=reason)
    try:
        found = await asyncio.to_thread(transcript_image, path, offset, n, flavor)
    except OSError as exc:
        logger.warning(
            "⚠️ transcript image %s (%s) read failed: %s", sid[:8], agent, exc.__class__.__name__
        )
        raise HTTPException(status_code=404, detail="read_failed") from exc
    if found is None:
        logger.info("ℹ️ transcript image %s (%s) unavailable at %d/%d", sid[:8], agent, offset, n)
        raise HTTPException(status_code=404, detail="image_not_found")
    data, media = found
    return Response(
        content=data, media_type=media,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )
