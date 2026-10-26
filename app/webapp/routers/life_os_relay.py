"""Life OS Telegram relay — a skill's bot steers its own conversations (#1639).

Mounted from ``life_os.py`` via ``include_router``, like
``life_os_channels.py``.

    GET  /api/life-os/channels/{id}/relay/conversations?q=  → find
    POST /api/life-os/channels/{id}/relay/resume            → resume (or the
                                                               session that has it open)
    POST /api/life-os/channels/{id}/relay/steer             → type a message into one
    GET  /api/life-os/channels/{id}/relay/read?session_id=  → its last reply
    POST /api/life-os/channels/{id}/relay/stop              → quit one it reopened
    GET  /api/life-os/channels/{id}/relay/status?chat=      → what it has open, and closed

A relay-enabled profile's bot (``"relay": true`` in ``channel_profiles.json``)
calls these through ``scripts/telegram_relay.py``. The bot stays the only
Telegram reader: it gets text back and posts it with its own reply tool, and a
resumed conversation launches the ordinary way, with no ``--channels`` and no
settings file, so it never starts a second plugin server or sees the token.

**Who may call.** The PC itself only, carrying the profile's relay token
(``X-Relay-Token``), which the launch wrote into that bot's settings ``env``
block and nowhere else (:func:`src.launching.channel_profiles.relay_token_matches`). The
token names the profile and so the skill: no route takes a skill from the
caller. The profile's bot session must be running.

**What it may touch.** A target session must be alive, must not be a Telegram
bot or the fleet chief, and must have open one of the caller's skill's indexed
conversations (by the capture header's session id). Every other session is
refused, whatever the caller says it is.

**Reuse.** Nothing here re-implements a launcher action: find is the
conversation index and the search CLI, resume is
:func:`~app.webapp.routers.life_os_conversations.resume_or_open`, steer is the
Board's own ``/input`` route and read is the Board's ``/exchange``. This module
adds the identity and scope checks, and names every outcome.

**Every reply names a ``state``**, success or not, so the bot can say what
happened and never fold a failure into "ok". Refusals are HTTP errors whose
``detail`` is ``{"state", "message"}``. The bot's own wording for each state
is life-os's to write.

**What it may close.** Only a session in this profile's
:mod:`src.life_os.relay_registry`, the conversations ``resume`` itself
launched for the bot, and only while it still passes the steer checks above.
A conversation resume found already open, a Life OS session started from the
app, a bot and the chief are never there. A stop quits gracefully once the
turn has ended and its capture is current, and never escalates to a kill.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, NoReturn, Optional

from fastapi import APIRouter, HTTPException, Request, Response

from src import audit, board
from src.clients import session as session_client
from src.life_os import relay_registry
from src.launching.channel_profiles import (
    ChannelProfile,
    LABEL_PREFIX,
    load_channel_profiles,
    profile_session_label,
    relay_token_matches,
)
from src.scanner import Skill
from src.webapp_config import WebappConfig

from app.webapp.middleware import is_pc_itself
from app.webapp.routers._helpers import audit_off_loop, client_ip, maybe_json
from app.webapp.routers.board import session_exchange
from app.webapp.routers.life_os_channels import RESTART_QUIT_WAIT_SECONDS, stop_until_gone
from app.webapp.routers.life_os_conversations import (
    _capture_mtime,
    _group_by_conversation,
    resume_or_open,
    skill_conversations,
)
from app.webapp.routers.life_os_search import (
    _MAX_QUERY_CHARS,
    _search_limit,
    run_conversation_search,
)
from app.webapp.routers.life_os_spawn import _resolve_skill
from app.webapp.routers.sessions import session_input

logger = logging.getLogger(__name__)
router = APIRouter()

TOKEN_HEADER = "x-relay-token"
_PREFIX = "/api/life-os/channels/{profile_id}/relay"
# Fields a found conversation carries back to the bot: what it needs to pick
# one and say which, nothing about where the files live.
_FOUND_FIELDS = ("path", "title", "topic", "summary", "date", "last_interaction",
                 "resumable", "resume_reason")
# The bot's per-chat key is opaque (it passes the Telegram chat id); the cap
# only bounds what one call can store.
_CHAT_MAX_CHARS = 64
# HTTP status for each stop outcome that did nothing.
_STOP_STATUS = {"not_relay_owned": 403, "gone": 409, "busy": 409,
                "capture_unconfirmed": 409, "stop_unconfirmed": 502}


def _refuse(status: int, state: str, message: str) -> NoReturn:
    raise HTTPException(status_code=status, detail={"state": state, "message": message})


def _chat(value: Any) -> str:
    """The bot's opaque chat key, ``""`` when it passed none."""
    if value is None or value == "":
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        _refuse(400, "refused", "The chat key must be a string or a number.")
    text = str(value).strip()
    if len(text) > _CHAT_MAX_CHARS:
        _refuse(400, "refused", "The chat key is too long.")
    return text


@dataclass(frozen=True)
class _Caller:
    cfg: WebappConfig
    profile: ChannelProfile
    skill: Skill
    sessions: List[Dict[str, Any]]


async def _caller(profile_id: str, request: Request) -> _Caller:
    """Authenticate the bot and load the live session list once.

    The order is deliberate: nothing about a profile (not even whether it
    exists) is said to a caller that is not the PC, and the token is checked
    before anything is read on its behalf.
    """
    host = request.client.host if request.client else ""
    if not is_pc_itself(host, request.headers):
        _refuse(403, "refused", "The relay answers only callers on this PC.")
    cfg: WebappConfig = request.app.state.webapp_config
    profiles, _problems = load_channel_profiles(
        getattr(request.app.state, "channel_profiles_path", None)
    )
    profile = next((p for p in profiles if p.id == profile_id), None)
    if profile is None:
        _refuse(404, "refused", f"Unknown channel profile: {profile_id}.")
    if not profile.relay:
        _refuse(403, "relay_off", f"Relay is off for {profile.label}.")
    if not relay_token_matches(profile, request.headers.get(TOKEN_HEADER, "")):
        logger.warning(f"⚠️ relay {profile.id}: refused a call with a wrong or missing token")
        _refuse(401, "refused",
                f"The relay token is not {profile.label}'s current one; restart the bot "
                "after turning relay on.")
    try:
        skill = _resolve_skill(cfg, profile.skill)
    except HTTPException:
        _refuse(404, "not_found", f"{profile.label}'s skill {profile.skill!r} was not found.")
    try:
        sessions = await asyncio.to_thread(session_client.list_sessions, cfg.session_host_port)
    except session_client.SessionHostError as exc:
        logger.warning(f"⚠️ relay {profile.id}: session host unreachable ({exc})")
        _refuse(503, "host_unreachable",
                "The session host can't be reached, so it isn't known which "
                "conversations are open. Nothing was done.")
    label = profile_session_label(profile.id)
    if not any(s.get("label") == label and s.get("alive", True) for s in sessions):
        _refuse(403, "refused", f"{profile.label}'s bot session is not running.")
    return _Caller(cfg=cfg, profile=profile, skill=skill, sessions=sessions)


def _live_ids(caller: _Caller) -> List[str]:
    return [str(s.get("session_id")) for s in caller.sessions if s.get("alive", True)]


async def _turn_facts(caller: _Caller) -> Dict[str, Dict[str, Any]]:
    """Per live session id: its Board ``status`` (``None`` when not known)
    and ``last_turn_at``, the newest real conversation line in its
    transcript (``None`` when that cannot be read). The PTY's own
    ``last_output_at`` is no measure here: a footer repaint moves it after
    the turn, and the capture, have ended."""
    state = await asyncio.to_thread(
        board.read_sessions_state, Path(caller.cfg.sessions_state_file))
    turned = await asyncio.to_thread(board.attach_last_turn, caller.sessions, state["rows"])
    facts = {}
    for sess in turned:
        sid = str(sess.get("session_id"))
        row = board.state_row_for_session(caller.sessions, state["rows"], sid) or {}
        status = row.get("status")
        facts[sid] = {"status": status if isinstance(status, str) else None,
                      "last_turn_at": sess.get("last_turn_at")}
    return facts


async def _target(caller: _Caller, sid: str) -> Dict[str, Any]:
    """The live session ``sid`` when it is on one of the caller's skill's
    conversations; a refusal naming why otherwise."""
    sess = next((s for s in caller.sessions if str(s.get("session_id")) == sid), None)
    if not sid or sess is None or not sess.get("alive", True):
        _refuse(404, "not_found", "No running session with that id.")
    label = str(sess.get("label") or "")
    if label.startswith(LABEL_PREFIX) or label == "chief":
        _refuse(403, "not_this_skill",
                "That session is a bot or the fleet chief; the relay only steers "
                f"{caller.skill.name}'s conversations.")
    groups = await _group_by_conversation(caller.cfg, caller.sessions)
    held = {conv for conv, owners in groups.items()
            if any(str(o.get("session_id")) == sid for o in owners)}
    mine = {str(r["sid"]).lower() for r in await skill_conversations(caller.cfg, caller.skill)
            if r["sid"]}
    if not held & mine:
        _refuse(403, "not_this_skill",
                f"That session is not on one of {caller.skill.name}'s conversations.")
    return sess


async def _audit(caller: _Caller, request: Request, action: str, **fields: Any) -> None:
    # Facts only: never the steer's text, the query or the reply.
    await audit_off_loop(
        audit.audit_event, "lifeos_relay", action=action, profile=caller.profile.id,
        skill=caller.skill.id, client=client_ip(request), **fields,
    )


@router.get(f"{_PREFIX}/conversations")
async def relay_find(profile_id: str, request: Request) -> Dict[str, Any]:
    """The skill's conversations, newest first, or the search hits for ``q``.

    Each row says whether it can be resumed and, when a session already has
    it open, that ``session_id`` (steer that one instead of resuming).
    ``state`` is ``found``, ``none`` or ``unavailable`` (search not working,
    with a ``reason``).
    """
    caller = await _caller(profile_id, request)
    query = (request.query_params.get("q") or "").strip()
    limit = _search_limit(request.query_params.get("limit"))
    if len(query) > _MAX_QUERY_CHARS:
        _refuse(400, "refused", "The search text is too long.")
    if query:
        reply = await run_conversation_search(caller.cfg, query, caller.skill.id, limit)
        if not reply["available"]:
            return {"state": "unavailable", "reason": reply["reason"], "conversations": []}
        rows = reply["results"]
    else:
        rows = (await skill_conversations(caller.cfg, caller.skill))[:limit]
    groups = await _group_by_conversation(caller.cfg, caller.sessions)
    found = []
    for row in rows:
        owners = groups.get(str(row.get("sid") or "").lower(), [])
        found.append({
            **{key: row.get(key) for key in _FOUND_FIELDS},
            # The search's match tier (0 title .. 3 full text), only when it sent one.
            **({"tier": row["tier"]} if "tier" in row else {}),
            "session_id": str(owners[-1].get("session_id")) if owners else "",
        })
    await _audit(caller, request, "find", searched=bool(query), hits=len(found))
    logger.info(f"ℹ️ relay {caller.profile.id}: find returned {len(found)} conversation(s)")
    return {"state": "found" if found else "none", "skill": caller.skill.id,
            "conversations": found}


@router.post(f"{_PREFIX}/resume")
async def relay_resume(profile_id: str, request: Request) -> Dict[str, Any]:
    """Resume one conversation (``{"path": ...}`` from find) or, with no path,
    the newest resumable one, on its own model and with no PC mirror window.

    ``state`` is ``resumed`` or ``opened`` (already open: that session is the
    one to steer, nothing new was launched). With ``"chat"``, the bot's key
    for one chat, a chat that already has a reopened conversation running
    gets that one back as ``open_in_chat`` and nothing is launched: one per
    chat. Only a ``resumed`` session joins the registry the relay may stop.
    """
    caller = await _caller(profile_id, request)
    body = await maybe_json(request)
    path = str(body.get("path") or "")
    chat = _chat(body.get("chat"))
    if chat:
        held = await asyncio.to_thread(
            relay_registry.entries, caller.profile.id, _live_ids(caller), chat=chat)
        if held:
            entry = held[-1]
            sid = str(entry.get("session_id"))
            await asyncio.to_thread(relay_registry.touch, caller.profile.id, sid, time.time())
            await _audit(caller, request, "resume", session=sid, outcome="open_in_chat")
            logger.info(f"ℹ️ relay {caller.profile.id}: this chat already has "
                        f"{sid[:8]} open; nothing launched")
            return {"state": "open_in_chat", "session_id": sid,
                    "conversation": entry.get("conversation", ""),
                    "title": entry.get("title", "")}
    rows = await skill_conversations(caller.cfg, caller.skill)
    if path:
        row = next((r for r in rows if r["path"] == path), None)
        if row is None:
            _refuse(404, "not_found", f"No conversation of {caller.skill.name} at that path.")
        if not (row["resumable"] and row["path"]):
            _refuse(409, "not_resumable",
                    row["resume_reason"] or "That conversation can't be resumed.")
    else:
        if not rows:
            _refuse(409, "not_resumable", f"{caller.skill.name} has no conversation yet.")
        row = next((r for r in rows if r["resumable"] and r["path"]), None)
        if row is None:
            _refuse(409, "not_resumable", f"None of {caller.skill.name}'s conversations can be resumed.")
    try:
        result = await resume_or_open(caller.skill, request, {}, row, mirror=False)
    except HTTPException as exc:
        state = {409: "not_resumable", 404: "not_found", 503: "host_unreachable"}.get(
            exc.status_code, "unknown")
        logger.warning(f"⚠️ relay {caller.profile.id}: resume failed ({exc.status_code})")
        _refuse(exc.status_code, state, str(exc.detail))
    session = result.get("session") or {}
    action = result.get("conversation_action")
    state = "opened" if action == "open" else "resumed"
    sid = str(session.get("session_id") or "")
    title = row.get("title") or row.get("topic") or ""
    registered = await _register(caller, state, sid, chat, row["path"], title)
    await _audit(caller, request, "resume", session=sid, outcome=state)
    logger.info(f"✅ relay {caller.profile.id}: conversation {state} in session {sid[:8]}")
    return {"state": state, "session_id": sid, "conversation": row["path"],
            "title": title, "registered": registered}


async def _register(caller: _Caller, state: str, sid: str, chat: str,
                    conversation: str, title: str) -> bool:
    """Record a session ``resume`` launched; an ``opened`` one is only
    touched when the relay already owns it, never added.

    Whether ``sid`` is in the registry now (the relay may stop it). A failed
    write leaves the session running and unowned, so never stoppable by the
    relay: the safe side."""
    try:
        if state == "resumed" and sid:
            await asyncio.to_thread(relay_registry.add, caller.profile.id, chat, sid,
                                    conversation, title, time.time())
            return True
        return await asyncio.to_thread(relay_registry.touch, caller.profile.id, sid,
                                       time.time())
    except OSError as exc:
        logger.warning(f"⚠️ relay {caller.profile.id}: registry write failed for "
                       f"{sid[:8]} ({exc.__class__.__name__}); the relay can't stop it")
        return False


@router.post(f"{_PREFIX}/steer")
async def relay_steer(profile_id: str, request: Request) -> Dict[str, Any]:
    """Type ``{"data"}`` into ``{"session_id"}`` and submit it — once.

    ``state``: ``delivered`` (the submit was confirmed), ``queued`` (the
    target is mid-turn; the launcher presses Enter when it settles),
    ``unconfirmed`` (sent, nothing verified it — read before assuming).
    Refusals: ``stranded`` (written but not taken, never auto-resend),
    ``gone`` (the session ended), ``host_unreachable``, ``unknown``. The
    relay never retries a steer: a resent one can run twice.
    """
    caller = await _caller(profile_id, request)
    body = await maybe_json(request)
    sid = str(body.get("session_id") or "")
    data = body.get("data")
    if not isinstance(data, str) or not data.strip():
        _refuse(400, "refused", "Nothing to send.")
    if body.get("submit", True) is not True:
        _refuse(400, "refused", "The relay always submits what it sends.")
    await _target(caller, sid)
    status = Response()
    try:
        verdict = await session_input(sid, request, status)
    except HTTPException as exc:
        state = {502: "stranded", 409: "gone", 503: "host_unreachable"}.get(
            exc.status_code, "unknown")
        await _audit(caller, request, "steer", session=sid, outcome=state)
        logger.warning(f"⚠️ relay {caller.profile.id}: steer to {sid[:8]} {state} "
                       f"({exc.status_code})")
        _refuse(exc.status_code, state, str(exc.detail))
    await asyncio.to_thread(relay_registry.touch, caller.profile.id, sid, time.time())
    reason = verdict.get("reason")
    if reason == session_client.INPUT_DEFERRED:
        state = "queued"
    elif verdict.get("submit_state") == "confirmed":
        state = "delivered"
    else:
        state = "unconfirmed"
    await _audit(caller, request, "steer", session=sid, outcome=state)
    logger.info(f"ℹ️ relay {caller.profile.id}: steer to {sid[:8]} {state}")
    return {"state": state, "session_id": sid, "reason": reason}


@router.get(f"{_PREFIX}/read")
async def relay_read(profile_id: str, request: Request) -> Dict[str, Any]:
    """The target's last assistant reply, its timestamp, and whether it is
    still working (``status`` from the Board's hook row: ``working``,
    ``idle``, ``needs-you`` or ``None`` when not known).

    ``state``: ``reply``, or ``unavailable`` with the exchange's ``reason``
    (``stale`` when only an old cached answer could be had): never old text
    passed off as new.
    """
    caller = await _caller(profile_id, request)
    sid = request.query_params.get("session_id", "")
    sess = await _target(caller, sid)
    exchange = await session_exchange(sid, request)
    state = await asyncio.to_thread(
        board.read_sessions_state, Path(caller.cfg.sessions_state_file))
    row = board.state_row_for_session(caller.sessions, state["rows"], sid) or {}
    status = row.get("status") if isinstance(row.get("status"), str) else None
    await _audit(caller, request, "read", session=sid)
    base = {"session_id": sid, "status": status,
            "last_output_at": sess.get("last_output_at")}
    if exchange.get("stale"):
        return {**base, "state": "unavailable", "reason": "stale"}
    if not exchange.get("available"):
        return {**base, "state": "unavailable", "reason": exchange.get("reason")}
    assistant = exchange.get("assistant") or {}
    return {**base, "state": "reply", "text": str(assistant.get("text") or ""),
            "timestamp": assistant.get("timestamp")}


# ------------------------------------------------- closing what it reopened
async def stop_conversation(caller: _Caller, sid: str, reason: str) -> Dict[str, str]:
    """Quit ``sid`` when the relay reopened it for this profile and it is
    safe to close; ``{"state", "message"}`` either way.

    ``stopped`` (confirmed gone, moved to the unreported stops with
    ``reason``); otherwise nothing was done: ``not_relay_owned``, ``gone``,
    ``busy`` (mid-turn, waiting on an answer, or not known which),
    ``capture_unconfirmed`` (the capture is older than the last turn, or
    either is unknown) or ``stop_unconfirmed`` (quit sent, not confirmed
    gone: the entry is kept). Only ever a graceful quit, never a kill, so the
    capture hook's delayed index can finish. Shared with the idle close.
    """
    entry = await asyncio.to_thread(relay_registry.find, caller.profile.id, sid) if sid else None
    if entry is None:
        return {"state": "not_relay_owned",
                "message": f"The relay did not reopen that session for "
                           f"{caller.profile.label}; it was left alone."}
    if sid not in _live_ids(caller):
        await asyncio.to_thread(relay_registry.forget, caller.profile.id, sid)
        return {"state": "gone", "message": "That conversation's session already ended."}
    try:
        await _target(caller, sid)
    except HTTPException as exc:
        logger.warning(f"⚠️ relay {caller.profile.id}: stop of {sid[:8]} refused, it no "
                       f"longer passes the relay's checks ({exc.detail})")
        return {"state": "not_relay_owned",
                "message": f"That session is no longer one of {caller.skill.name}'s "
                           "conversations; it was left alone."}
    facts = (await _turn_facts(caller)).get(sid, {})
    status = facts.get("status")
    if status != "idle":
        why = {"working": "it is mid-turn", "needs-you": "it is waiting on an answer"}.get(
            status or "", "whether it is mid-turn is not known")
        return {"state": "busy", "message": f"Not stopped: {why}. Nothing was done."}
    captured = await asyncio.to_thread(
        _capture_mtime, Path(caller.cfg.life_os_dir), str(entry.get("conversation") or ""))
    last_turn = facts.get("last_turn_at")
    if not captured or last_turn is None or captured < last_turn:
        logger.info(f"ℹ️ relay {caller.profile.id}: stop of {sid[:8]} waits, capture at "
                    f"{captured or 'none'} vs last turn at {last_turn}")
        return {"state": "capture_unconfirmed",
                "message": "Not stopped: its last turn is not confirmed saved yet. "
                           "Nothing was done."}
    what = f"relay {caller.profile.id} stop"
    try:
        gone = await stop_until_gone(caller.cfg.session_host_port, sid, "quit",
                                     RESTART_QUIT_WAIT_SECONDS, what)
    except session_client.SessionHostError as exc:
        logger.warning(f"⚠️ {what}: session-host stopped answering ({exc}); entry kept")
        return {"state": "stop_unconfirmed",
                "message": "Quit sent, but the session host stopped answering, so it is "
                           "not confirmed closed."}
    if not gone:
        logger.warning(f"⚠️ {what}: {sid[:8]} still alive after the quit; entry kept, "
                       "no kill")
        return {"state": "stop_unconfirmed",
                "message": "Quit sent, but the session is still running."}
    try:
        await asyncio.to_thread(relay_registry.record_stop, caller.profile.id, sid,
                                reason, time.time())
    except OSError as exc:
        logger.warning(f"⚠️ {what}: closed, but the stop was not recorded "
                       f"({exc.__class__.__name__})")
    logger.info(f"✅ {what}: {sid[:8]} closed ({reason})")
    return {"state": "stopped",
            "message": f"Closed {entry.get('title') or 'the conversation'}."}


@router.post(f"{_PREFIX}/stop")
async def relay_stop(profile_id: str, request: Request) -> Dict[str, Any]:
    """Close ``{"session_id"}``, a conversation the relay reopened for this
    bot, because the owner said the chat is done. ``state`` is ``stopped``;
    every other outcome is a refusal that did nothing (see
    :func:`stop_conversation`)."""
    caller = await _caller(profile_id, request)
    body = await maybe_json(request)
    sid = str(body.get("session_id") or "")
    result = await stop_conversation(caller, sid, "done")
    await _audit(caller, request, "relay_stop", session=sid, outcome=result["state"])
    if result["state"] != "stopped":
        logger.info(f"ℹ️ relay {caller.profile.id}: stop of {sid[:8]} {result['state']}")
        _refuse(_STOP_STATUS[result["state"]], result["state"], result["message"])
    return {**result, "session_id": sid}


def _iso(stamp: Any) -> str:
    if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
        return ""
    return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat(timespec="seconds")


def _stamp(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def last_active_at(entry: Dict[str, Any], sess: Dict[str, Any],
                   facts: Dict[str, Any]) -> Optional[float]:
    """When a reopened conversation was last used, ``None`` when nothing says.

    The newest of the relay's own last call on it, its transcript's last
    turn, its PTY's last output, the last input written to it and its
    composer's last keystroke, so use from the app counts as much as the
    bot's. Shared by ``status`` and the idle close."""
    last_input = sess.get("last_input") if isinstance(sess.get("last_input"), dict) else {}
    stamps = [_stamp(v) for v in (
        entry.get("last_activity"), facts.get("last_turn_at"), sess.get("last_output_at"),
        last_input.get("at"), sess.get("composer_dirty_since"),
    )]
    known = [s for s in stamps if s is not None]
    return max(known) if known else None


@router.get(f"{_PREFIX}/status")
async def relay_status(profile_id: str, request: Request) -> Dict[str, Any]:
    """This profile's reopened conversations still running (one chat's with
    ``?chat=``), each with ``idle_minutes`` and ``minutes_to_close`` (``None``
    when idle close is off or the idle time is unknown), plus the ``stops``
    recorded since the previous status call, each with its ``reason``
    (``done`` or ``idle``). ``state`` is ``open`` or ``none``; stops are
    reported either way, and each is reported once."""
    caller = await _caller(profile_id, request)
    chat = _chat(request.query_params.get("chat")) or None
    held = await asyncio.to_thread(
        relay_registry.entries, caller.profile.id, _live_ids(caller), chat=chat)
    facts = await _turn_facts(caller) if held else {}
    by_sid = {str(s.get("session_id")): s for s in caller.sessions}
    now = time.time()
    limit = caller.profile.idle_close_minutes
    open_rows = []
    for entry in held:
        sid = str(entry.get("session_id"))
        active = last_active_at(entry, by_sid.get(sid, {}), facts.get(sid, {}))
        idle = int((now - active) // 60) if active is not None else None
        open_rows.append({
            "session_id": sid, "conversation": entry.get("conversation", ""),
            "title": entry.get("title", ""), "chat": entry.get("chat", ""),
            "idle_minutes": idle,
            "minutes_to_close": max(0, limit - idle) if limit and idle is not None else None,
        })
    stops = await asyncio.to_thread(relay_registry.take_stops, caller.profile.id, chat=chat)
    await _audit(caller, request, "status", open=len(open_rows), stops=len(stops))
    return {
        "state": "open" if open_rows else "none",
        "conversations": open_rows,
        "stops": [{**{key: stop.get(key, "") for key in
                      ("session_id", "conversation", "title", "chat", "reason")},
                   "stopped_at": _iso(stop.get("stopped_at"))} for stop in stops],
    }
