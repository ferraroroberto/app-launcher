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

``POST /api/claude-code/sessions/{sid}/answer`` answers the session's
pending ``AskUserQuestion`` from the Chat pane's question card (#1149):
re-checks at send time that the call is still the one waiting, turns the
pick into the picker's own keystrokes (``src.ask_user_question``) and types
them — raw, one key per write — over the PTY socket, or one console-input
call per key for a detached session. Same gate as ``/input``; its own
``_TERMINAL_GUARD_RULES`` row.

``GET .../plan-picker`` and ``POST .../plan-answer`` answer Claude Code's
plan picker (#1151). They read the terminal's screen, not the transcript:
the session's PTY capture rendered at the PTY's size (``src.plan_picker``).
The POST reads it again at send time and types only while the tapped digit
still carries the tapped label. Full-control sessions only, and each route
has its own ``_TERMINAL_GUARD_RULES`` row.

Unavailable sources are told apart on purpose (``reason``): a session the
host doesn't know, an agent with no structured history, a history file that
isn't there, and a file that *is* there but couldn't be read. Bodies are
never logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, Request
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import InvalidHandshake, WebSocketException

from src import audit, board, plan_picker, session_client
from src.ask_user_question import TOOL_NAME as ASK_TOOL_NAME, answer_keystrokes
from src.board_transcript import pending_decision_call
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

from app.webapp.routers._helpers import audit_off_loop, maybe_json
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
) -> Tuple[Optional[str], Optional[str], Optional[Path], str, str, Optional[Dict[str, Any]]]:
    """Session lookup + history-path resolution, shared by every route here.

    Returns ``(reason, flavor, path, agent, why, session)`` — ``reason`` is
    set (and ``flavor``/``path`` are ``None``) exactly when the caller must
    respond ``_unavailable(sid, reason)`` instead of reading the source.
    ``why`` is how the path was resolved or, for ``no_transcript``, which
    guard refused (``""`` where a flavour has nothing to add). ``session``
    is the session-host's row, ``None`` only for ``session_not_found``.
    """
    live, state = await asyncio.gather(
        asyncio.to_thread(_safe_list_sessions, cfg.session_host_port),
        asyncio.to_thread(board.read_sessions_state, Path(cfg.sessions_state_file)),
    )
    session = next(
        (item for item in live if str(item.get("session_id")) == str(sid)), None
    )
    if session is None:
        return "session_not_found", None, None, "", "", None
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
    reason, flavor, path, agent, why, _session = await _resolve_source(sid, cfg)
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


# The gap between two keystrokes of one answer. The picker takes one key per
# input event, so each key is its own write; 0.35 s keeps two writes from
# landing in one read even on a loaded box, and a whole answer (at most four
# questions) still types in a few seconds.
_ANSWER_KEY_GAP_S = 0.35

# The card shows the refusal as-is, so it is worded for the reader.
_NOT_WAITING = "This question is no longer waiting for an answer"


class _PartialAnswer(Exception):
    """A write failed after ``sent`` of ``total`` had already gone in."""

    def __init__(self, sent: int, total: int, detail: str) -> None:
        super().__init__(detail)
        self.sent, self.total, self.detail = sent, total, detail


async def _type_into_pty(port: int, sid: str, keys: List[Tuple[str, bool]]) -> None:
    """Raw keystrokes over the session-host socket — the same ``input``
    frame the terminal's own key bar sends, so no bracketed-paste framing.
    ``role=pc`` so this short-lived connection never claims the PTY's size;
    whatever the host streams back is drained and dropped."""
    writes = [w for text, enter in keys for w in ([text, "\r"] if enter else [text])]
    sent = 0
    try:
        async with ws_connect(session_client.ws_url(port, sid, "pc"), max_size=None) as ws:
            async def drain() -> None:
                async for _ in ws:
                    pass

            drainer = asyncio.create_task(drain())
            try:
                for data in writes:
                    if sent:
                        await asyncio.sleep(_ANSWER_KEY_GAP_S)
                    await ws.send(json.dumps({"type": "input", "data": data}))
                    sent += 1
                # Let the last frame reach the host before the close does.
                await asyncio.sleep(_ANSWER_KEY_GAP_S)
            finally:
                drainer.cancel()
    except (OSError, InvalidHandshake, WebSocketException) as exc:
        raise _PartialAnswer(sent, len(writes), f"terminal socket failed: {exc}") from exc


async def _type_into_console(port: int, sid: str, keys: List[Tuple[str, bool]]) -> None:
    """One console-input call per step for a detached session: the helper
    types each as key records (and presses Enter itself for a typed answer)."""
    for sent, (text, enter) in enumerate(keys):
        if sent:
            await asyncio.sleep(_ANSWER_KEY_GAP_S)
        try:
            await asyncio.to_thread(session_client.send_input, port, sid, text, enter)
        except session_client.SessionHostError as exc:
            raise _PartialAnswer(sent, len(keys), str(exc)) from exc


@router.post("/api/claude-code/sessions/{sid}/answer")
async def session_answer(sid: str, request: Request) -> Dict[str, Any]:
    """Answer the session's pending ``AskUserQuestion`` (#1149).

    Body: ``{"tool_use_id": str, "answers": [...]}`` — one answer per
    question, shapes in :func:`src.ask_user_question.answer_keystrokes`.

    Checked **at send time**, not trusted from the card: the call named must
    be the newest decision call still unresolved in the transcript
    (:func:`src.board_transcript.pending_decision_call`, the definition the
    Board's ``awaiting-decision`` status uses), or it is a 409 and nothing
    is typed. The keys are built from the transcript's own copy of the
    questions, never the client's.

    ``delivered`` is ``"unconfirmed"`` on success: nothing here can see the
    picker take the keys. The agent's ``tool_result`` landing in the
    transcript is the confirmation, and the Chat pane's live refresh shows
    it. A failure part-way is a 502 that says how many writes went in, since
    a half-typed answer leaves a picker state the user has to look at.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    body = await maybe_json(request)
    call_id = body.get("tool_use_id")
    if not isinstance(call_id, str) or not call_id:
        raise HTTPException(status_code=400, detail="tool_use_id must be a string")
    reason, flavor, path, _agent, _why, session = await _resolve_source(sid, cfg)
    if reason == "session_not_found":
        raise HTTPException(status_code=409, detail="This session is no longer running")
    if flavor != "claude" or session is None:
        # Only Claude Code has the tool; any other reason means there is no
        # transcript to check against, which must stop the send all the same.
        raise HTTPException(status_code=409, detail=_NOT_WAITING)
    pending = await asyncio.to_thread(pending_decision_call, path)
    if not pending or pending["name"] != ASK_TOOL_NAME or pending["id"] != call_id:
        logger.info("ℹ️ answer %s refused: question ...%s is not the pending one", sid[:8], call_id[-8:])
        raise HTTPException(status_code=409, detail=_NOT_WAITING)
    try:
        keys = answer_keystrokes(pending["input"].get("questions"), body.get("answers"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    kind = str(session.get("kind") or "pty")
    deliver = _type_into_console if kind == "remote" else _type_into_pty
    try:
        await deliver(cfg.session_host_port, sid, keys)
    except _PartialAnswer as exc:
        # Counts only — never the answer, which may be private.
        await audit_off_loop(
            audit.session_log, sid, "answer", kind=kind, steps=len(keys),
            reason="error", sent=exc.sent, detail=exc.detail[:200],
        )
        logger.info(
            "⚠️ answer %s failed after %d/%d writes: %s", sid[:8], exc.sent, exc.total, exc.detail[:200]
        )
        where = "the PC console" if kind == "remote" else "the terminal"
        raise HTTPException(
            status_code=502,
            detail=(
                "Answer not sent: nothing reached the question" if exc.sent == 0
                else f"Answer only partly sent ({exc.sent} of {exc.total} keys): check {where}"
            ),
        ) from exc
    await audit_off_loop(
        audit.session_log, sid, "answer", kind=kind, steps=len(keys), reason="unverified"
    )
    logger.info("⌨️ answer %s typed (%s, %d steps); the transcript confirms it", sid[:8], kind, len(keys))
    return {"ok": True, "delivered": "unconfirmed", "steps": len(keys), "kind": kind}


# --- Plan picker (#1151) -----------------------------------------------------
#
# The transcript can't say "this plan is waiting" (the pending ExitPlanMode
# call can stay off disk until it is answered), so both routes read the
# terminal's screen instead: the session's PTY capture rendered at the PTY's
# own size (src.plan_picker). Full-control sessions only — a detached one has
# no screen here to read, so its plan is answered in the PC console.

# The last picker state logged per session, so a poll every few seconds
# leaves one line per change rather than one per tick.
_PICKER_SEEN: Dict[str, str] = {}


def _no_picker(reason: str) -> Dict[str, Any]:
    return {"available": False, "showing": False, "answerable": False,
            "reason": reason, "options": [], "cursor": None,
            "plan": None, "plan_source": None, "plan_truncated": False}


async def _read_picker(
    cfg: WebappConfig, sid: str
) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """``(reason, session, picker)``: ``reason`` says why there is no screen
    to read; otherwise ``picker`` is what it shows (``None``: no picker)."""
    live = await asyncio.to_thread(_safe_list_sessions, cfg.session_host_port)
    session = next((s for s in live if str(s.get("session_id")) == str(sid)), None)
    if session is None:
        return "session_not_found", None, None
    if str(session.get("agent") or "claude").lower() != "claude":
        return "unsupported_agent", session, None
    if str(session.get("kind") or "pty") == "remote":
        return "detached", session, None

    def read() -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        text = plan_picker.read_capture_tail(audit.transcript_path(sid))
        if text is None:
            return "no_screen", None
        rows = int(session.get("rows") or 40)
        cols = int(session.get("cols") or 120)
        return None, plan_picker.parse_picker(plan_picker.screen_lines(text, rows, cols))

    reason, picker = await asyncio.to_thread(read)
    return reason, session, picker


def _log_picker(sid: str, state: str, detail: str = "") -> None:
    if _PICKER_SEEN.get(sid) == state:
        return
    _PICKER_SEEN[sid] = state
    logger.info("ℹ️ plan picker %s: %s%s", sid[:8], state, f" ({detail})" if detail else "")


@router.get("/api/claude-code/sessions/{sid}/plan-picker")
async def session_plan_picker(sid: str, request: Request) -> Dict[str, Any]:
    """Whether the session's terminal shows Claude Code's plan picker now,
    and its options exactly as the screen lists them (Tailscale + passkey).

    Polled by the Chat pane while a full-control Claude session is open.
    ``reason`` tells apart a session that is gone, a detached one (no screen
    to read), a capture that can't be read, and a screen with no picker.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    reason, _session, picker = await _read_picker(cfg, sid)
    if reason is not None:
        if reason == "session_not_found":
            _PICKER_SEEN.pop(sid, None)
        else:
            _log_picker(sid, reason)
        return _no_picker(reason)
    if picker is None:
        _log_picker(sid, "not showing")
        return {**_no_picker("not_showing"), "available": True}
    plan = await asyncio.to_thread(plan_picker.read_plan_file, picker["plan_file"])
    source = "file" if plan else ("screen" if picker["plan_excerpt"] else None)
    text, truncated = plan if plan else (picker["plan_excerpt"] or None, False)
    _log_picker(
        sid, "showing" if picker["answerable"] else "showing, not answerable",
        f"{len(picker['options'])} options, plan from {source or 'nowhere'}",
    )
    return {
        "available": True, "showing": True, "answerable": picker["answerable"],
        "reason": None, "options": picker["options"], "cursor": picker["cursor"],
        "plan": text, "plan_source": source, "plan_truncated": truncated,
    }


@router.post("/api/claude-code/sessions/{sid}/plan-answer")
async def session_plan_answer(sid: str, request: Request) -> Dict[str, Any]:
    """Answer the plan picker on a full-control session's terminal (#1151).

    Body: ``{"option": n, "label": str, "feedback"?: str}`` — the digit and
    the label the card showed for it. The screen is read again here, at
    send time, and the keys go in only when that digit still carries that
    label and the picker can take a digit (``src.plan_picker.answer_keys``);
    otherwise it is a 409 and nothing is typed. A "Yes" option is its digit;
    the feedback option is its digit, the text, then Enter.

    ``delivered`` is ``"unconfirmed"``: the picker closing (next poll) and
    the call's result in the transcript are the confirmation.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    body = await maybe_json(request)
    reason, session, picker = await _read_picker(cfg, sid)
    if reason == "session_not_found":
        raise HTTPException(status_code=409, detail="This session is no longer running")
    if reason == "detached":
        raise HTTPException(
            status_code=409,
            detail="Chat can't see a detached session's screen: answer the plan in the PC console",
        )
    if reason is not None:
        raise HTTPException(
            status_code=409,
            detail="Chat can't see the plan picker on the terminal, so nothing was sent",
        )
    try:
        keys = plan_picker.answer_keys(
            picker, body.get("option"), body.get("label"), body.get("feedback")
        )
    except plan_picker.PickerChanged as exc:
        logger.info("ℹ️ plan answer %s refused: %s", sid[:8], exc)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    kind = "feedback" if len(keys) > 1 else "approve"
    try:
        await _type_into_pty(cfg.session_host_port, sid, keys)
    except _PartialAnswer as exc:
        # Counts only — never the feedback, which may be private.
        await audit_off_loop(
            audit.session_log, sid, "plan_answer", kind=kind, steps=len(keys),
            reason="error", sent=exc.sent, detail=exc.detail[:200],
        )
        logger.info(
            "⚠️ plan answer %s failed after %d/%d writes: %s",
            sid[:8], exc.sent, exc.total, exc.detail[:200],
        )
        raise HTTPException(
            status_code=502,
            detail=(
                "Answer not sent: nothing reached the terminal" if exc.sent == 0
                else f"Answer only partly sent ({exc.sent} of {exc.total} keys): check the terminal"
            ),
        ) from exc
    await audit_off_loop(
        audit.session_log, sid, "plan_answer", kind=kind, steps=len(keys), reason="unverified"
    )
    logger.info("⌨️ plan answer %s typed (%s, option %s)", sid[:8], kind, body.get("option"))
    return {"ok": True, "delivered": "unconfirmed", "answer": kind}
