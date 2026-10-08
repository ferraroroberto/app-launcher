"""Coding tab: a live session's terminal screen — read it, and type into it
(split out of ``session_transcript.py``, #1424).

None of these routes read the transcript's content. They read the PTY's screen
(``src.plan_picker``) and/or type keystrokes into the PTY, so they share
nothing with the transcript pager but the session lookup
(``session_transcript._resolve_source``, used by ``/answer``).

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

``GET .../context`` reads the same screen for the session's context-window
use (#1223): the ``NN%c`` the fleet statusline paints under Claude Code's
prompt (``src.statusline_context``), which the overlay bar draws as a ring.
Same screen read as the plan picker, same reasons, its own guard row.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import InvalidHandshake, WebSocketException

from src import audit, plan_picker, resume_picker, session_client
from src.ask_user_question import TOOL_NAME as ASK_TOOL_NAME, answer_keystrokes
from src.board_transcript import pending_decision_call
from src.statusline_context import context_percent
from src.webapp_config import WebappConfig

from app.webapp.routers._helpers import audit_off_loop, maybe_json
from app.webapp.routers.board_spawn import SESSION_HOST_UNREACHABLE, _read_live_sessions
from app.webapp.routers.session_transcript import _resolve_source

logger = logging.getLogger(__name__)

router = APIRouter()

# The gap between two keystrokes of one answer. The picker takes one key per
# input event, so each key is its own write; 0.35 s keeps two writes from
# landing in one read even on a loaded box, and a whole answer (at most four
# questions) still types in a few seconds.
_ANSWER_KEY_GAP_S = 0.35

# The card shows the refusal as-is, so it is worded for the reader.
_NOT_WAITING = "This question is no longer waiting for an answer"


def _refuse_if_host_unreachable(reason: Optional[str]) -> None:
    """503 for a POST whose session lookup could not reach the session-host
    (#1308) — not the 409 "no longer running" a session that is gone gets,
    since this one may well be alive and the caller should retry."""
    if reason == SESSION_HOST_UNREACHABLE:
        raise HTTPException(
            status_code=503, detail="The session host is unreachable, so nothing was sent"
        )


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
    _refuse_if_host_unreachable(reason)
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


# --- The terminal's screen: plan picker (#1151), context use (#1223) --------
#
# The transcript can't say "this plan is waiting" (the pending ExitPlanMode
# call can stay off disk until it is answered) or how full the context
# window is (it never names the window's size), so these routes read the
# terminal's screen instead: the session's PTY capture rendered at the PTY's
# own size (src.plan_picker). Full-control Claude sessions only — a detached
# one has no screen here to read, so its plan is answered in the PC console.

# The last state logged per session, one map per route, so a poll every few
# seconds leaves one line per change rather than one per tick.
_PICKER_SEEN: Dict[str, str] = {}
_CONTEXT_SEEN: Dict[str, str] = {}


def _no_picker(reason: str, resume_showing: bool = False) -> Dict[str, Any]:
    return {"available": False, "showing": False, "answerable": False,
            "reason": reason, "options": [], "cursor": None,
            "plan": None, "plan_source": None, "plan_truncated": False,
            "resume_picker": resume_showing}


async def _read_screen(
    cfg: WebappConfig, sid: str
) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[List[str]]]:
    """``(reason, session, lines)``: ``reason`` says why there is no screen
    to read (``session_not_found`` / ``session_host_unreachable`` (#1308) /
    ``unsupported_agent`` / ``detached`` / ``no_screen``); otherwise ``lines``
    is the screen as the PTY shows it."""
    live, host_error = await asyncio.to_thread(_read_live_sessions, cfg.session_host_port)
    session = next((s for s in live if str(s.get("session_id")) == str(sid)), None)
    if session is None:
        return (SESSION_HOST_UNREACHABLE if host_error else "session_not_found"), None, None
    if str(session.get("agent") or "claude").lower() != "claude":
        return "unsupported_agent", session, None
    if str(session.get("kind") or "pty") == "remote":
        return "detached", session, None

    def read() -> Optional[List[str]]:
        text = plan_picker.read_capture_tail(audit.transcript_path(sid))
        if text is None:
            return None
        rows = int(session.get("rows") or 40)
        cols = int(session.get("cols") or 120)
        return plan_picker.screen_lines(text, rows, cols)

    lines = await asyncio.to_thread(read)
    if lines is None:
        return "no_screen", session, None
    return None, session, lines


async def _read_picker(
    cfg: WebappConfig, sid: str
) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """``(reason, session, picker)``: ``reason`` as :func:`_read_screen`;
    otherwise ``picker`` is what the screen shows (``None``: no picker)."""
    reason, session, lines = await _read_screen(cfg, sid)
    if reason is not None:
        return reason, session, None
    return None, session, plan_picker.parse_picker(lines)


def _log_screen(seen: Dict[str, str], what: str, sid: str, state: str, detail: str = "") -> None:
    if seen.get(sid) == state:
        return
    seen[sid] = state
    logger.info("ℹ️ %s %s: %s%s", what, sid[:8], state, f" ({detail})" if detail else "")


def _log_picker(sid: str, state: str, detail: str = "") -> None:
    _log_screen(_PICKER_SEEN, "plan picker", sid, state, detail)


@router.get("/api/claude-code/sessions/{sid}/plan-picker")
async def session_plan_picker(sid: str, request: Request) -> Dict[str, Any]:
    """Whether the session's terminal shows Claude Code's plan picker now,
    and its options exactly as the screen lists them (Tailscale + passkey).

    Polled by the Chat pane while a full-control Claude session is open.
    ``reason`` tells apart a session that is gone, a detached one (no screen
    to read), a capture that can't be read, and a screen with no picker.
    The same screen read also says whether the ``/resume`` picker is up
    (``resume_picker``, #1300), so the Chat pane needs no second poll to
    offer its resume card.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    reason, _session, lines = await _read_screen(cfg, sid)
    if reason is not None:
        if reason == "session_not_found":
            _PICKER_SEEN.pop(sid, None)
        else:
            _log_picker(sid, reason)
        return _no_picker(reason)
    picker = plan_picker.parse_picker(lines)
    if picker is None:
        resume_showing = resume_picker.parse_resume_picker(lines)
        _log_picker(sid, "resume picker showing" if resume_showing else "not showing")
        return {**_no_picker("not_showing", resume_showing), "available": True}
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
        "resume_picker": False,
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
    _refuse_if_host_unreachable(reason)
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


# --- Resuming a conversation from a Chat card (#1300) ------------------------
#
# Claude Code's /resume picker is a long scrolling TUI list; the Chat pane
# offers the project's conversations as a searchable card instead
# (src.resume_picker). With no picker on screen a pick is typed as
# `/resume <id>` + Enter, which the probe on #1300 showed resumes exactly that
# conversation. With the picker up it is never left with Escape, which exits
# a `claude --resume` launch outright: the highlight is stepped down one row
# at a time, re-read after each, and Enter goes in only once the highlighted
# row can be nothing but the pick. The list and the id are checked again at
# send time, and the result is read back off the terminal rather than assumed.

# How long a pick waits for the terminal to say what happened, and how often
# it looks. Resuming a long conversation takes a few seconds to repaint.
_RESUME_CONFIRM_S = 10.0
_RESUME_POLL_S = 0.5
# How long one Down gets to repaint the picker before it is read anyway (two
# neighbouring rows can look alike, so an unchanged screen is no failure).
_RESUME_STEP_S = 1.5
# Steering re-reads far more often than a read-back: a long list takes one
# step per row, and each step waits on this.
_RESUME_STEP_POLL_S = 0.1
_RESUME_VIA = ("picker", "composer")
# Why a steered pick stopped short of Enter: nothing was selected, and the
# only keys sent moved the highlight.
_STEER_REFUSALS = {
    "closed": "The resume picker closed on the terminal: nothing was selected",
    "ambiguous": "The picker shows too little of that title to tell it from another session: "
                 "pick it on the terminal. Nothing was selected",
    "missing": "That session isn't in the terminal's picker list: nothing was selected",
    "unreadable": "Chat lost sight of the terminal's screen while finding that session: "
                  "nothing was selected",
}


async def _wait_for_screen(
    cfg: WebappConfig, sid: str, check, timeout: float, poll: Optional[float] = None
) -> Any:
    """Re-read the terminal every ``poll`` seconds (default
    :data:`_RESUME_POLL_S`) until ``check(lines, session)`` answers something
    truthy, or ``timeout`` passes (then ``None``)."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while True:
        reason, session, lines = await _read_screen(cfg, sid)
        if reason is None:
            found = check(lines, session)
            if found:
                return found
        if loop.time() >= end:
            return None
        await asyncio.sleep(_RESUME_POLL_S if poll is None else poll)


async def _steer_picker(
    cfg: WebappConfig, sid: str, lines: List[str], sessions: List[Dict[str, Any]], target: str
) -> Tuple[str, int]:
    """Move the ``/resume`` picker's highlight onto ``target``: ``(verdict,
    downs_sent)``, the verdict ``"target"`` (Enter is safe) or a key of
    :data:`_STEER_REFUSALS`.

    One Down per step, the screen re-read after each. Down wraps from the
    last row to the first, so the list's length plus one steps visit every
    row from any start (the search box included). Only arrow keys are sent.
    A socket failure raises :class:`_PartialAnswer` counting every Down sent.
    """
    total = resume_picker.picker_total(lines)
    budget = min(max(total or 0, len(sessions)) + 1, resume_picker.LIST_CAP + 1)
    downs = 0
    ambiguous = False
    while True:
        verdict = resume_picker.cursor_on(lines, sessions, target) if lines is not None else "unreadable"
        if verdict in ("target", "closed", "unreadable"):
            return verdict, downs
        ambiguous = ambiguous or verdict == "ambiguous"
        if downs >= budget:
            return ("ambiguous" if ambiguous else "missing"), downs
        try:
            await _type_into_pty(cfg.session_host_port, sid, [(resume_picker.DOWN, False)])
        except _PartialAnswer as exc:
            # Count the Downs that did go in, so the caller never says
            # "nothing reached the terminal" about a moved highlight.
            raise _PartialAnswer(downs + exc.sent, budget, exc.detail) from exc
        downs += 1
        before = lines
        lines = await _wait_for_screen(
            cfg, sid, lambda ls, _s: ls if ls != before else None, _RESUME_STEP_S,
            poll=_RESUME_STEP_POLL_S,
        )
        if lines is None:
            _reason, _session, lines = await _read_screen(cfg, sid)


@router.get("/api/claude-code/sessions/{sid}/resume-sessions")
async def session_resume_sessions(sid: str, request: Request) -> Dict[str, Any]:
    """The conversations ``/resume`` offers for this session's project,
    newest first (``[{"id", "title", "updated_at"}]``), and whether the
    ``/resume`` picker is on its screen now (Tailscale + passkey: the titles
    come from the transcripts). Full-control Claude sessions only, like the
    plan card: a pick has to be typed and read back on this screen."""
    cfg: WebappConfig = request.app.state.webapp_config
    reason, session, lines = await _read_screen(cfg, sid)
    if reason is not None:
        return {"available": False, "reason": reason, "picker": False, "sessions": []}
    project_dir = str(session.get("project_dir") or "")
    sessions = await asyncio.to_thread(resume_picker.list_sessions, project_dir)
    if sessions is None:
        logger.info("⚠️ resume list %s: projects folder unreadable", sid[:8])
        return {"available": False, "reason": "no_sessions_folder", "picker": False, "sessions": []}
    return {
        "available": True, "reason": None,
        "picker": resume_picker.parse_resume_picker(lines), "sessions": sessions,
    }


@router.post("/api/claude-code/sessions/{sid}/resume")
async def session_resume(sid: str, request: Request) -> Dict[str, Any]:
    """Resume one of the project's conversations on this session's terminal.

    Body: ``{"session_id": str, "via": "picker" | "composer"}``. ``picker``:
    the card was offered because the ``/resume`` picker is up, and it must
    still be up now, or nothing is typed. ``composer``: the user typed
    ``/resume`` in Chat; refused while the plan picker holds the terminal.
    Either way the id must be one ``/resume`` offers here right now
    (:func:`src.resume_picker.resume_keys`).

    ``outcome``, read back off the terminal: ``resumed`` (the window title
    switched to the picked conversation's), ``not_found`` / ``cancelled``
    (Claude Code answered the command with that line), or ``unconfirmed``
    (neither within the wait: a conversation with no title of its own
    never renames the window).
    """
    cfg: WebappConfig = request.app.state.webapp_config
    body = await maybe_json(request)
    via = body.get("via")
    if via not in _RESUME_VIA:
        raise HTTPException(status_code=400, detail="via must be 'picker' or 'composer'")
    reason, session, lines = await _read_screen(cfg, sid)
    _refuse_if_host_unreachable(reason)
    if reason == "session_not_found":
        raise HTTPException(status_code=409, detail="This session is no longer running")
    if reason == "detached":
        raise HTTPException(
            status_code=409,
            detail="Chat can't see a detached session's screen: resume it in the PC console",
        )
    if reason is not None:
        raise HTTPException(
            status_code=409, detail="Chat can't see the terminal's screen, so nothing was sent"
        )
    showing = resume_picker.parse_resume_picker(lines)
    if via == "picker" and not showing:
        raise HTTPException(
            status_code=409, detail="The resume picker closed on the terminal: nothing was sent"
        )
    if not showing and plan_picker.parse_picker(lines) is not None:
        raise HTTPException(
            status_code=409, detail="The terminal is waiting on a plan: answer it first"
        )
    sessions = await asyncio.to_thread(
        resume_picker.list_sessions, str(session.get("project_dir") or "")
    )
    target = body.get("session_id")
    try:
        if showing:
            title = resume_picker.steer_check(sessions, target)["title"]
            keys = [(resume_picker.ENTER, False)]
        else:
            title = resume_picker.pick_session(sessions, target)["title"]
            keys = resume_picker.resume_keys(sessions, target)
    except resume_picker.ResumeRefused as exc:
        logger.info("ℹ️ resume %s refused: %s", sid[:8], exc)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    downs = 0
    steering = showing
    try:
        if showing:
            verdict, downs = await _steer_picker(cfg, sid, lines, sessions, target)
            steering = False
            if verdict != "target":
                await audit_off_loop(
                    audit.session_log, sid, "resume", via=via, steps=downs, reason=f"steer_{verdict}",
                )
                logger.info("⚠️ resume %s: picker not steered onto the pick (%s, %d downs)",
                            sid[:8], verdict, downs)
                raise HTTPException(status_code=409, detail=_STEER_REFUSALS[verdict])
        await _type_into_pty(cfg.session_host_port, sid, keys)
    except _PartialAnswer as exc:
        # A failure while steering already counts its Downs; one on the final
        # write adds to the Downs that went in before it.
        sent = exc.sent if steering else downs + exc.sent
        await audit_off_loop(
            audit.session_log, sid, "resume", via=via, steps=downs + len(keys),
            reason="error", sent=sent, detail=exc.detail[:200],
        )
        logger.info("⚠️ resume %s failed after %d writes: %s", sid[:8], sent, exc.detail[:200])
        if sent == 0:
            detail = "Resume not sent: nothing reached the terminal"
        elif showing:
            detail = "Resume not sent: the picker was moved but nothing was selected, check the terminal"
        else:
            detail = "Resume not sent: the command did not go in completely, check the terminal"
        raise HTTPException(status_code=502, detail=detail) from exc
    steps = downs + len(keys)

    want = resume_picker._fold(title)

    def read_back(ls: List[str], live: Optional[Dict[str, Any]]) -> Optional[str]:
        said = resume_picker.resume_outcome(ls, target)
        if said:
            return said
        live_title = resume_picker._fold((live or {}).get("live_title"))
        return "resumed" if want and want in live_title else None

    outcome = await _wait_for_screen(cfg, sid, read_back, _RESUME_CONFIRM_S) or "unconfirmed"
    await audit_off_loop(audit.session_log, sid, "resume", via=via, steps=steps, reason=outcome)
    logger.info("⌨️ resume %s typed (%s, %d steps): %s", sid[:8], via, steps, outcome)
    return {"ok": True, "outcome": outcome, "title": title}



@router.get("/api/claude-code/sessions/{sid}/context")
async def session_context(sid: str, request: Request) -> Dict[str, Any]:
    """How full the session's context window is, as its terminal's
    statusline shows it now (Tailscale + passkey, #1223).

    ``{"available", "percent", "reason"}``. ``percent`` is an int only when
    the bottom-most statusline on screen shows ``NN%c``; ``reason`` tells
    apart a session that is gone, a non-Claude agent, a detached one (no
    screen to read), a capture that can't be read (``no_screen``), and a
    screen whose footer shows no context figure (``not_showing``: no
    statusline, or Claude Code's own null early on and right after
    ``/compact``). The overlay bar polls it and draws nothing without a
    number, so an unknown value is never shown as 0%.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    reason, _session, lines = await _read_screen(cfg, sid)
    if reason is not None:
        if reason == "session_not_found":
            _CONTEXT_SEEN.pop(sid, None)
        else:
            _log_screen(_CONTEXT_SEEN, "context", sid, reason)
        return {"available": False, "percent": None, "reason": reason}
    percent = context_percent(lines)
    if percent is None:
        _log_screen(_CONTEXT_SEEN, "context", sid, "not showing")
        return {"available": True, "percent": None, "reason": "not_showing"}
    _log_screen(_CONTEXT_SEEN, "context", sid, "showing", f"{percent}%")
    return {"available": True, "percent": percent, "reason": None}
