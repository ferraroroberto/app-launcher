"""Heal a Telegram channel session whose plugin server is gone.

Every :data:`TICK_SECONDS` the canonical webapp checks each running channel
profile with :mod:`src.launching.channel_health`. A profile that stays
``degraded`` (alive, but no plugin server beneath its Claude process) for
:data:`HEAL_AFTER_SECONDS` is restarted once, through the restart route's own
:func:`~app.webapp.routers.life_os_channels.restart_channel_session`. If it is
degraded again for that long after the restart, the watcher stops acting and
flags the owner: the channel list reports ``needs_owner`` with the reason, and
a warning line and an audit event record it. It never loops.

A profile that reports ``ok`` clears its record, so a later fault gets its one
restart again. ``starting`` and ``unknown`` neither advance nor clear it: a
fresh session is too young to judge, and an unread process table is not
evidence either way. A profile that is not running clears it too; starting a
stopped bot is the owner's call or the boot autostart's, never this loop's.

Canonical instance and listener owner only (see ``app.webapp.server``): a
disposable gate webapp has its own empty session-host, and two webapp
processes must not both restart one bot.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from fastapi import HTTPException

from app.webapp.route_pools import AUDIT, LIFE_OS_FILES, run_on
from app.webapp.routers import life_os_channels
from src import audit
from src.clients import session as session_client
from src.launching import channel_health
from src.launching.channel_profiles import ChannelProfile, load_channel_profiles

logger = logging.getLogger(__name__)

TICK_SECONDS = 60.0
# How long a profile stays degraded before its one restart, and again before
# the owner is flagged: long enough that a server restarting itself is not
# taken for a dead one, short enough that a deaf bot is back within minutes.
HEAL_AFTER_SECONDS = 180.0
FLAG_REASON_SUFFIX = "; one automatic restart did not bring it back"


@dataclass
class HealState:
    """What the watcher remembers between ticks, per profile id."""
    degraded_since: Dict[str, float] = field(default_factory=dict)
    restarted_at: Dict[str, float] = field(default_factory=dict)
    # Profile id → the reason the owner was flagged.
    flagged: Dict[str, str] = field(default_factory=dict)

    def clear(self, profile_id: str) -> bool:
        """Forget the profile; ``True`` when there was anything to forget."""
        had = any(profile_id in d for d in (
            self.degraded_since, self.restarted_at, self.flagged))
        self.degraded_since.pop(profile_id, None)
        self.restarted_at.pop(profile_id, None)
        self.flagged.pop(profile_id, None)
        return had


async def _restart(cfg: Any, profile: ChannelProfile) -> bool:
    try:
        await life_os_channels.restart_channel_session(
            cfg, profile, body={}, request=None, client="channel-heal",
        )
    except HTTPException as exc:
        logger.warning(
            f"⚠️ channel heal: profile {profile.id} restart refused "
            f"({exc.status_code}: {exc.detail})"
        )
        return False
    return True


async def _audit(action: str, profile_id: str, **fields: Any) -> None:
    await run_on(AUDIT, audit.audit_event, "channel_heal",
                 action=action, profile=profile_id, **fields)


async def _heal_profile(
    cfg: Any, profile: ChannelProfile, health: Optional[str], reason: str,
    moment: float, state: HealState,
) -> None:
    pid = profile.id
    if health is None or health == channel_health.OK:
        if state.clear(pid) and health == channel_health.OK:
            logger.info(f"ℹ️ channel heal: profile {pid} plugin server is running again")
        return
    if health != channel_health.DEGRADED or pid in state.flagged:
        return
    if pid not in state.degraded_since:
        state.degraded_since[pid] = moment
        logger.info(f"ℹ️ channel heal: profile {pid} degraded: {reason}")
        await _audit("detected", pid, reason=reason)
    if moment - state.degraded_since[pid] < HEAL_AFTER_SECONDS:
        return
    if pid not in state.restarted_at:
        state.restarted_at[pid] = moment
        state.degraded_since.pop(pid, None)
        logger.info(
            f"ℹ️ channel heal: profile {pid} degraded for "
            f"{HEAL_AFTER_SECONDS:.0f}s; restarting it once"
        )
        restarted = await _restart(cfg, profile)
        await _audit("restart", pid, reason=reason, restarted=restarted)
        return
    flag = reason + FLAG_REASON_SUFFIX
    state.flagged[pid] = flag
    logger.warning(f"⚠️ channel heal: profile {pid} still degraded after its "
                   f"automatic restart; flagged for the owner: {flag}")
    await _audit("flag_owner", pid, reason=flag)


async def tick_once(
    cfg: Any,
    profiles_path: Optional[Path],
    state: HealState,
    now: Optional[float] = None,
    scan: Callable[[], Optional[List[channel_health.ProcRow]]] = channel_health.snapshot,
) -> None:
    """One pass over every running profile."""
    if cfg is None:
        return
    profiles, _problems = await run_on(LIFE_OS_FILES, load_channel_profiles, profiles_path)
    if not profiles:
        return
    try:
        live = await life_os_channels._live_profile_sessions(cfg)
    except session_client.SessionHostError as exc:
        logger.info(f"ℹ️ channel heal: session host unreachable ({exc}); next tick")
        return
    procs = await run_on(LIFE_OS_FILES, scan) if live else None
    servers = channel_health.server_states([p.id for p in profiles], procs or [])
    moment = time.time() if now is None else now
    for profile in profiles:
        health, reason = channel_health.assess(
            live.get(profile.id), servers.get(profile.id), procs is not None, moment
        )
        await _heal_profile(cfg, profile, health, reason, moment, state)


async def tick_forever(app: Any) -> None:
    """The lifespan loop; config and profiles are re-read every tick, and the
    state lives on ``app.state.channel_heal`` so the channel list can report
    a flag."""
    state = app.state.channel_heal = HealState()
    while True:
        await asyncio.sleep(TICK_SECONDS)
        try:
            await tick_once(getattr(app.state, "webapp_config", None),
                            getattr(app.state, "channel_profiles_path", None), state)
        except Exception:  # noqa: BLE001 — never take the lifespan down
            logger.exception("❌ channel heal tick failed")
