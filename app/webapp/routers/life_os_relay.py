"""Life OS Telegram relay — a skill's bot steers its own conversations (#1639).

Mounted from ``life_os.py`` via ``include_router``, like
``life_os_channels.py``.

    GET  /api/life-os/channels/{id}/relay/conversations?q=  → find
    POST /api/life-os/channels/{id}/relay/resume            → resume (or the
                                                               session that has it open)
    POST /api/life-os/channels/{id}/relay/steer             → type a message into one
    GET  /api/life-os/channels/{id}/relay/read?session_id=  → its last reply

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
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, NoReturn

from fastapi import APIRouter, HTTPException, Request, Response

from src import audit, board
from src.clients import session as session_client
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
from app.webapp.routers.life_os_conversations import (
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


def _refuse(status: int, state: str, message: str) -> NoReturn:
    raise HTTPException(status_code=status, detail={"state": state, "message": message})


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
    one to steer, nothing new was launched).
    """
    caller = await _caller(profile_id, request)
    body = await maybe_json(request)
    path = str(body.get("path") or "")
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
    await _audit(caller, request, "resume", session=sid, outcome=state)
    logger.info(f"✅ relay {caller.profile.id}: conversation {state} in session {sid[:8]}")
    return {"state": state, "session_id": sid, "conversation": row["path"],
            "title": row.get("title") or row.get("topic") or ""}


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
