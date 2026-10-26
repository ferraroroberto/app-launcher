"""Idle close of the conversations the Telegram relay reopened for a bot.

Every :data:`TICK_SECONDS` the canonical webapp walks the relay registry
(:mod:`src.life_os.relay_registry`) and, for each profile whose
``idle_close_minutes`` is set, closes an entry that has seen nothing for that
long. Idle time runs from the newest of: the relay's own last call on it
(``last_activity``), the transcript's last turn, the PTY's last output, the
last input written to it and the composer's last keystroke. A conversation the
owner is also using from the app is therefore not idle, and one with typed but
unsent text (``composer_dirty`` true or unknown) is never closed.

Only the registry is read, so a Life OS session launched from the app, a bot,
the chief and a conversation the relay merely found open are never candidates.
A due entry closes through the relay's own
:func:`~app.webapp.routers.life_os_relay.stop_conversation`: the same
ownership, busy and capture checks, and a graceful quit, never a kill.
``busy``, ``capture_unconfirmed`` and ``composer_not_empty`` wait for the
next tick, logged once per change; after
:data:`CAPTURE_TRIES` unconfirmed captures in a row the entry is left alone
until it shows new activity.

On ``stopped`` the stop is recorded with reason ``idle`` (``status`` lists it)
and one fixed line, :data:`NOTICE_PREFIX` plus the title, is typed into the
profile's bot session, once and never resent. The line carries no
instructions: the bot confirms it through ``status`` before telling the chat.
A notice that cannot be delivered leaves the recorded stop for the bot's next
``status`` call. The launcher never talks to Telegram.

Canonical instance only: a disposable gate webapp has its own empty
session-host, and must never close or type into a real session.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from src import audit
from src.clients import session as session_client
from src.launching.channel_profiles import (
    ChannelProfile,
    load_channel_profiles,
    profile_session_label,
)
from src.life_os import relay_registry

from app.webapp.routers import life_os_relay
from app.webapp.routers.life_os_spawn import _resolve_skill

logger = logging.getLogger(__name__)

TICK_SECONDS = 60.0
# Unconfirmed captures in a row before the entry is left alone: three minutes
# is well past the capture hook's own write after a turn ends.
CAPTURE_TRIES = 3
NOTICE_PREFIX = "[launcher-relay] closed after idle: "
# The title rides on one typed line; the cap keeps it one short line.
_TITLE_MAX_CHARS = 120
# Outcomes that leave the entry for the next tick.
_WAITING = ("busy", "capture_unconfirmed", "composer_not_empty")


@dataclass
class ReaperState:
    """What the reaper remembers between ticks, per session id."""
    # (idle stamp the count belongs to, unconfirmed captures in a row)
    capture_misses: Dict[str, Tuple[float, int]] = field(default_factory=dict)
    # Last outcome logged and audited, so a wait repeats nothing per tick.
    last_outcome: Dict[str, str] = field(default_factory=dict)


def _notice(title: str) -> str:
    one_line = " ".join(str(title or "the conversation").split())
    return NOTICE_PREFIX + (one_line[:_TITLE_MAX_CHARS] or "the conversation")


async def _send_notice(caller: Any, title: str) -> str:
    """Type the stop notice into the profile's bot session, once; the outcome."""
    label = profile_session_label(caller.profile.id)
    bot = next((s for s in caller.sessions
                if s.get("label") == label and s.get("alive", True)), None)
    if bot is None:
        return "bot_not_running"
    bot_sid = str(bot.get("session_id"))
    text = _notice(title)
    try:
        verdict = await asyncio.to_thread(
            session_client.send_input, caller.cfg.session_host_port, bot_sid, text, True)
    except session_client.SessionHostError as exc:
        logger.warning(f"⚠️ relay {caller.profile.id} idle close: notice to the bot not "
                       f"delivered ({exc.status}); its next status call reports the stop")
        await asyncio.to_thread(audit.session_log, bot_sid, "input", bytes=len(text),
                                submit=True, reason="error", detail=str(exc)[:200])
        return "failed"
    reason = (verdict or {}).get("reason") if isinstance(verdict, dict) else None
    await asyncio.to_thread(audit.session_log, bot_sid, "input", bytes=len(text),
                            submit=True, reason=reason, preview=text[:120])
    return "deferred" if reason == session_client.INPUT_DEFERRED else "sent"


async def _close_profile(cfg: Any, profile: ChannelProfile, sessions: List[Dict[str, Any]],
                         now: float, state: ReaperState) -> None:
    by_sid = {str(s.get("session_id")): s for s in sessions if s.get("alive", True)}
    held = await asyncio.to_thread(relay_registry.entries, profile.id, list(by_sid))
    if not held:
        return
    try:
        skill = _resolve_skill(cfg, profile.skill)
    except HTTPException:
        logger.warning(f"⚠️ relay {profile.id} idle close: skill {profile.skill!r} not "
                       "found; nothing closed")
        return
    caller = life_os_relay._Caller(cfg=cfg, profile=profile, skill=skill, sessions=sessions)
    facts = await life_os_relay._turn_facts(caller)
    limit = profile.idle_close_minutes * 60
    for entry in held:
        sid = str(entry.get("session_id"))
        sess = by_sid[sid]
        active = life_os_relay.last_active_at(entry, sess, facts.get(sid, {}))
        if active is None or now - active < limit:
            state.capture_misses.pop(sid, None)
            state.last_outcome.pop(sid, None)
            continue
        misses = state.capture_misses.get(sid)
        if misses and misses[0] == active and misses[1] >= CAPTURE_TRIES:
            continue
        idle_minutes = int((now - active) // 60)
        if sess.get("composer_dirty") is not False:
            # Typed but unsent text, or not known whether there is any: a quit
            # would throw it away.
            outcome = "composer_not_empty"
        else:
            outcome = (await life_os_relay.stop_conversation(caller, sid, "idle"))["state"]
        if outcome == "capture_unconfirmed":
            count = misses[1] + 1 if misses and misses[0] == active else 1
            state.capture_misses[sid] = (active, count)
            if count >= CAPTURE_TRIES:
                logger.warning(f"⚠️ relay {profile.id} idle close: {sid[:8]}'s capture "
                               f"unconfirmed {count} times; left alone until it is used again")
        else:
            state.capture_misses.pop(sid, None)
        notice = await _send_notice(caller, str(entry.get("title") or "")) \
            if outcome == "stopped" else None
        if outcome in _WAITING and state.last_outcome.get(sid) == outcome:
            continue
        state.last_outcome[sid] = outcome
        fields = {"notice": notice} if notice else {}
        await asyncio.to_thread(
            audit.audit_event, "lifeos_relay", action="relay_idle_stop", profile=profile.id,
            skill=skill.id, session=sid, idle_minutes=idle_minutes, outcome=outcome, **fields)
        logger.info(f"ℹ️ relay {profile.id} idle close: {sid[:8]} idle {idle_minutes} min, "
                    f"{outcome}" + (f", notice {notice}" if notice else ""))


async def tick_once(cfg: Any, profiles_path: Optional[Path], state: ReaperState,
                    now: Optional[float] = None) -> None:
    """One pass over every profile's reopened conversations."""
    if cfg is None:
        return
    profiles, _problems = load_channel_profiles(profiles_path)
    due = [p for p in profiles if p.idle_close_minutes > 0]
    if not due:
        return
    try:
        sessions = await asyncio.to_thread(session_client.list_sessions, cfg.session_host_port)
    except session_client.SessionHostError as exc:
        logger.info(f"ℹ️ relay idle close: session host unreachable ({exc}); next tick")
        return
    moment = time.time() if now is None else now
    for profile in due:
        await _close_profile(cfg, profile, sessions, moment, state)


async def tick_forever(app: Any) -> None:
    """The lifespan loop; config and profiles are re-read every tick."""
    state = ReaperState()
    while True:
        await asyncio.sleep(TICK_SECONDS)
        try:
            await tick_once(getattr(app.state, "webapp_config", None),
                            getattr(app.state, "channel_profiles_path", None), state)
        except Exception:  # noqa: BLE001 — never take the lifespan down
            logger.exception("❌ relay idle close tick failed")
