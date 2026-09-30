"""Life OS Telegram channel profiles — list + launch (issue #1366).

Split off ``life_os.py`` the way ``life_os_conversations.py`` and
``life_os_files.py`` were (#884): mounted from there via ``include_router``, so
``app/webapp/server.py`` still registers one ``life_os.router``.

    GET  /api/life-os/channels                    → the profiles + which are running,
                                                     and the setup checks (#1369)
    POST /api/life-os/channels/{id}/launch        → start (or resume) a profile's
                                                     Claude session with the
                                                     Telegram channel attached
                                                     (Tailscale + passkey)

A profile (:mod:`src.channel_profiles`) binds one life-os skill to one Telegram
bot. The launch opens that skill exactly as the skill tile does, plus
``--channels`` and a per-profile settings file that hands the bot's state
directory to that one session. Claude only, and never detached: a detached
console carries neither the session label the one-live-session guard reads nor a
terminal to watch.

**One live session per profile.** A bot token allows one ``getUpdates`` consumer,
so a second session on the same profile would fight the first with a 409. The
guard looks the profile's label up in the session-host's live list, under a
per-profile lock so two quick taps cannot both pass it.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from src import session_client
from src.channel_profiles import (
    ChannelProfile,
    bun_available,
    env_file_present,
    label_profile_id,
    load_channel_profiles,
    plugin_installed_for,
    profile_session_label,
    profiles_file_present,
    write_channel_settings,
)
from src.launch_flags import build_claude_flags
from src.scanner import scan_skills
from src.webapp_config import WebappConfig

from app.webapp.routers._helpers import maybe_json
from app.webapp.routers.life_os_spawn import (
    _resolve_launch_choice,
    _resolve_skill,
    _spawn_skill_session,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_launch_locks: Dict[str, asyncio.Lock] = {}


def _profiles(request: Request) -> tuple[List[ChannelProfile], List[str]]:
    """Load the profiles; ``app.state.channel_profiles_path`` overrides the
    default file (tests point it at a temp file)."""
    return load_channel_profiles(
        getattr(request.app.state, "channel_profiles_path", None)
    )


async def _live_profile_sessions(cfg: WebappConfig) -> Dict[str, Dict[str, Any]]:
    """Alive channel sessions by profile id. Raises ``SessionHostError`` when
    the host cannot be asked — callers must not read that as "none running"."""
    sessions = await asyncio.to_thread(
        session_client.list_sessions, cfg.session_host_port
    )
    live: Dict[str, Dict[str, Any]] = {}
    for sess in sessions:
        pid = label_profile_id(sess.get("label"))
        if pid and sess.get("alive", True):
            live.setdefault(pid, sess)
    return live


@router.get("/api/life-os/channels")
async def list_channels(request: Request) -> Dict[str, Any]:
    """The configured profiles, each with the session it is running in, plus the
    setup checks the Settings card shows (#1369).

    ``running`` is ``None`` (not ``False``) for every profile when the
    session-host cannot be reached: an unknown is not "nothing running". The
    same rule holds for ``setup.plugin`` (``None`` = Claude's install record
    could not be read). Only booleans and names leave here: no state
    directory, no path, no file content — ``env_present`` is a stat of the
    token file, which is never opened.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    profiles, problems = _profiles(request)
    life_os_dir = Path(cfg.life_os_dir)
    skills = scan_skills(life_os_dir) if life_os_dir.is_dir() else []
    skill_ids = {s.id for s in skills}
    live: Optional[Dict[str, Dict[str, Any]]] = None
    if profiles:
        try:
            live = await _live_profile_sessions(cfg)
        except session_client.SessionHostError as exc:
            logger.debug(f"channel profile session lookup failed: {exc}")
    rows = []
    for profile in profiles:
        sess = (live or {}).get(profile.id)
        rows.append({
            "id": profile.id,
            "label": profile.label,
            "skill": profile.skill,
            "skill_found": profile.skill in skill_ids,
            "env_present": env_file_present(profile),
            "running": None if live is None else bool(sess),
            "session_id": str((sess or {}).get("session_id") or ""),
        })
    return {
        "profiles": rows,
        "problems": problems,
        "setup": {
            "file_present": profiles_file_present(
                getattr(request.app.state, "channel_profiles_path", None)
            ),
            "life_os_found": life_os_dir.is_dir(),
            "bun": bun_available(),
            "plugin": plugin_installed_for(
                life_os_dir,
                getattr(request.app.state, "installed_plugins_path", None),
            ),
            "skills": [{"id": s.id, "name": s.name} for s in skills],
        },
    }


@router.post("/api/life-os/channels/{profile_id}/launch")
async def launch_channel(profile_id: str, request: Request) -> Dict[str, Any]:
    """Launch (or, with ``resume``, resume into) a profile's channel session.

    Body: ``{"model": str, "resume": bool, "rows": int, "cols": int}``. Resume
    opens Claude's native ``/resume`` picker instead of the skill, with the same
    channel flags and settings file, so the bot comes back attached to the
    conversation the picker selects.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    life_os_dir = Path(cfg.life_os_dir)
    if not life_os_dir.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"life_os_dir does not exist: {cfg.life_os_dir}",
        )
    profiles, _problems = _profiles(request)
    profile = next((p for p in profiles if p.id == profile_id), None)
    if profile is None:
        raise HTTPException(
            status_code=404, detail=f"unknown channel profile: {profile_id}"
        )
    body = await maybe_json(request)
    if str(body.get("mode") or "pty").strip().lower() == "remote":
        raise HTTPException(
            status_code=400,
            detail="Telegram channel sessions run in a terminal, not detached",
        )
    agent, model = _resolve_launch_choice(body)
    if agent != "claude":
        raise HTTPException(
            status_code=400,
            detail="Telegram channel sessions run on Claude only",
        )
    skill = _resolve_skill(cfg, profile.skill)
    resume = bool(body.get("resume", False))

    lock = _launch_locks.setdefault(profile.id, asyncio.Lock())
    async with lock:
        try:
            live = await _live_profile_sessions(cfg)
        except session_client.SessionHostError as exc:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Can't check whether {profile.label} is already running: "
                    f"the session-host is unreachable ({exc})"
                ),
            )
        running = live.get(profile.id)
        if running:
            name = running.get("name") or "session"
            logger.info(
                f"ℹ️ channel profile {profile.id} launch refused: already "
                f"running in session {str(running.get('session_id'))[:8]}"
            )
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{profile.label} is already running in session "
                    f"“{name}” ({str(running.get('session_id') or '')[:8]}). "
                    "One Telegram bot can only be attached to one session."
                ),
            )
        try:
            settings_path = write_channel_settings(profile)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        base = build_claude_flags(
            cfg, model_override=model, channel_settings=settings_path
        )
        flags = f"{base} /resume" if resume else f"{base} /{skill.command}"
        result = await _spawn_skill_session(
            cfg, request, life_os_dir,
            flags=flags, name=skill.name, kind="pty", agent="claude",
            model=model, resume=resume, audit_skill=skill.id, body=body,
            label=profile_session_label(profile.id),
        )
    logger.info(
        f"✅ channel profile {profile.id} launched ({'resume' if resume else 'skill'})"
    )
    return {"launched": skill.id, "profile": profile.id, **result}
