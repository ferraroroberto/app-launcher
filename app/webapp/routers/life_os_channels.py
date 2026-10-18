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
    POST /api/life-os/channels/{id}/restart       → quit the running session,
                                                     confirm it is gone, start
                                                     it fresh (#1675)

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

**Restart (#1675).** A running bot keeps the skill text, settings, hooks and
model it started with. Restart quits it and starts a fresh session under the
same lock, so a Launch can't slip in between; when the quit can't be
confirmed it starts nothing and answers 502.

**Boot-time autostart (#1542).** With ``autostart_channel_sessions`` on, the
webapp's lifespan calls :func:`autostart_channel_sessions`, which starts each
ready profile through :func:`start_channel_session` — the launch route's own
body, so the lock and the already-running refusal hold for it too.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from src import audit, session_client
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
from src.launcher import close_mirror_window
from src.scanner import Skill, scan_skills
from src.webapp_config import WebappConfig

from app.webapp.routers._helpers import (
    audit_off_loop,
    cert_present,
    client_ip,
    maybe_json,
)
from app.webapp.routers.life_os_spawn import (
    _resolve_launch_choice,
    _resolve_skill,
    _spawn_skill_session,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_launch_locks: Dict[str, asyncio.Lock] = {}

# Restart (#1675): how long a quit gets to end the old session before the
# kill, how long the kill gets, and the poll between looks.
RESTART_QUIT_WAIT_SECONDS = 15.0
RESTART_KILL_WAIT_SECONDS = 5.0
RESTART_POLL_SECONDS = 0.5


def _profiles(request: Request) -> tuple[List[ChannelProfile], List[str]]:
    """Load the profiles; ``app.state.channel_profiles_path`` overrides the
    default file (tests point it at a temp file)."""
    return load_channel_profiles(
        getattr(request.app.state, "channel_profiles_path", None)
    )


def relay_base_url(cfg: WebappConfig) -> str:
    """The loopback URL a relay-enabled bot calls this webapp on (#1639).

    Loopback, so the request is the PC itself; the relay routes still demand
    the profile's token on top of that.
    """
    return f"{'https' if cert_present() else 'http'}://127.0.0.1:{cfg.port}"


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
            "relay": profile.relay,
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
    # No model named (and no legacy ``opus`` flag): the channel default, not
    # the global Claude model (#1673). An explicit model still wins.
    if body.get("model") is None and "opus" not in body:
        body = {**body, "model": cfg.channel_model}
    agent, model = _resolve_launch_choice(body)
    if agent != "claude":
        raise HTTPException(
            status_code=400,
            detail="Telegram channel sessions run on Claude only",
        )
    return await start_channel_session(
        cfg, profile, model=model, resume=bool(body.get("resume", False)),
        body=body, request=request,
    )


async def start_channel_session(
    cfg: WebappConfig,
    profile: ChannelProfile,
    *,
    model: str,
    resume: bool,
    body: Dict[str, Any],
    request: Optional[Request],
) -> Dict[str, Any]:
    """Start one profile's channel session — the launch route's body, shared
    with the boot-time autostart (#1542) so both go through the per-profile
    lock and the already-running refusal: never two sessions on one bot.

    ``request`` is ``None`` for the autostart, which has no client to audit
    and no one watching, so it opens no PC mirror window (as the fleet
    chief's unwatched dispatch, #1283). Refusals raise ``HTTPException``
    with the same status and detail the route returns.
    """
    life_os_dir, skill = _profile_skill(cfg, profile)
    async with _launch_locks.setdefault(profile.id, asyncio.Lock()):
        _refuse_if_running(profile, await _live_or_503(cfg, profile))
        result = await _spawn_channel(
            cfg, profile, skill, life_os_dir,
            model=model, resume=resume, body=body, request=request,
        )
    logger.info(
        f"✅ channel profile {profile.id} launched ({'resume' if resume else 'skill'})"
    )
    return {"launched": skill.id, "profile": profile.id, **result}


def _profile_skill(cfg: WebappConfig, profile: ChannelProfile) -> tuple[Path, Skill]:
    """The life-os folder and the profile's skill, or the 400/404 refusal."""
    life_os_dir = Path(cfg.life_os_dir)
    if not life_os_dir.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"life_os_dir does not exist: {cfg.life_os_dir}",
        )
    return life_os_dir, _resolve_skill(cfg, profile.skill)


async def _live_or_503(
    cfg: WebappConfig, profile: ChannelProfile
) -> Dict[str, Dict[str, Any]]:
    """The live channel sessions, or a 503: an unreachable host is never read
    as "nothing running"."""
    try:
        return await _live_profile_sessions(cfg)
    except session_client.SessionHostError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Can't check whether {profile.label} is already running: "
                f"the session-host is unreachable ({exc})"
            ),
        )


def _refuse_if_running(
    profile: ChannelProfile, live: Dict[str, Dict[str, Any]]
) -> None:
    """The 409 for a profile that already has a live session."""
    running = live.get(profile.id)
    if not running:
        return
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


async def _spawn_channel(
    cfg: WebappConfig,
    profile: ChannelProfile,
    skill: Skill,
    life_os_dir: Path,
    *,
    model: str,
    resume: bool,
    body: Dict[str, Any],
    request: Optional[Request],
) -> Dict[str, Any]:
    """Write the profile's settings file and spawn its session. The caller
    holds the profile's lock and has confirmed nothing is running."""
    try:
        settings_path = write_channel_settings(profile, relay_url=relay_base_url(cfg))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    base = build_claude_flags(
        cfg, model_override=model, channel_settings=settings_path
    )
    flags = f"{base} /resume" if resume else f"{base} /{skill.command}"
    return await _spawn_skill_session(
        cfg, request, life_os_dir,
        flags=flags, name=skill.name, kind="pty", agent="claude",
        model=model, resume=resume, audit_skill=skill.id, body=body,
        label=profile_session_label(profile.id), mirror=request is not None,
    )


# --- restart (#1675) -------------------------------------------------------
@router.post("/api/life-os/channels/{profile_id}/restart")
async def restart_channel(profile_id: str, request: Request) -> Dict[str, Any]:
    """Quit a profile's running session and start it fresh with its skill, so
    it picks up the current SKILL.md, settings, hooks and model.

    Body: ``{"rows": int, "cols": int}``. No model: a restart always uses
    ``channel_model`` (#1673). A stopped profile is simply started
    (``restarted: false``). The quit, the confirmation that the old session
    is gone and the new start all run under the profile's launch lock, so a
    Launch arriving meanwhile waits and then gets the usual 409. When the old
    session can't be confirmed gone (still alive after the kill, or the host
    stopped answering), this answers 502 and starts nothing: never two
    sessions on one bot.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    profiles, _problems = _profiles(request)
    profile = next((p for p in profiles if p.id == profile_id), None)
    if profile is None:
        raise HTTPException(
            status_code=404, detail=f"unknown channel profile: {profile_id}"
        )
    body = await maybe_json(request)
    _agent, model = _resolve_launch_choice({"model": cfg.channel_model})
    life_os_dir, skill = _profile_skill(cfg, profile)
    async with _launch_locks.setdefault(profile.id, asyncio.Lock()):
        running = (await _live_or_503(cfg, profile)).get(profile.id)
        old_sid = str((running or {}).get("session_id") or "")
        if old_sid:
            await _quit_and_confirm_gone(cfg, profile, old_sid)
            # The old session is gone; anything still live on this profile
            # is another session, and a start would make two.
            _refuse_if_running(profile, await _live_or_503(cfg, profile))
        logger.info(f"ℹ️ channel profile {profile.id} restart: starting fresh")
        result = await _spawn_channel(
            cfg, profile, skill, life_os_dir,
            model=model, resume=False, body=body, request=request,
        )
    new_sid = str((result.get("session") or {}).get("session_id") or "")
    await audit_off_loop(
        audit.audit_event, "channel_restart",
        profile=profile.id, old_session=old_sid, session=new_sid,
        client=client_ip(request),
    )
    logger.info(
        f"✅ channel profile {profile.id} "
        f"{'restarted' if old_sid else 'started (was not running)'} "
        f"in session {new_sid[:8]}"
    )
    return {
        "launched": skill.id, "profile": profile.id,
        "restarted": bool(old_sid), **result,
    }


async def _wait_until_gone(port: int, sid: str, wait_s: float) -> bool:
    """Poll the session until it is gone or no longer alive, at most
    ``wait_s``. ``False`` means still alive at the deadline; an unreachable
    host raises ``SessionHostError`` (a 404 is the session gone)."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            info = await asyncio.to_thread(session_client.get_session, port, sid)
        except session_client.SessionHostError as exc:
            if exc.status == 404:
                return True
            raise
        if not info.get("alive", True):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(RESTART_POLL_SECONDS)


async def _quit_and_confirm_gone(
    cfg: WebappConfig, profile: ChannelProfile, sid: str
) -> None:
    """Quit the session, escalating to one kill; return only once it is
    confirmed gone, else raise the 502 that keeps a restart from starting."""
    port = cfg.session_host_port
    # The PC mirror window, as the Coding stop route closes it. Best effort.
    try:
        await asyncio.to_thread(close_mirror_window, sid)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"⚠️ mirror window close raised for {sid[:8]}: {exc}")
    started = time.monotonic()
    failed = (
        f"Couldn't confirm {profile.label} stopped; it was not started again."
    )
    for mode, wait_s in (
        ("quit", RESTART_QUIT_WAIT_SECONDS), ("kill", RESTART_KILL_WAIT_SECONDS),
    ):
        try:
            await asyncio.to_thread(session_client.stop, port, sid, mode)
        except session_client.SessionHostError as exc:
            if exc.status == 404:
                logger.info(
                    f"ℹ️ channel profile {profile.id} restart: session "
                    f"{sid[:8]} already gone at {mode}"
                )
                return
            # The poll below decides: a stop that timed out may still land.
            logger.info(
                f"ℹ️ channel profile {profile.id} restart: {mode} of "
                f"{sid[:8]} answered {exc.status} ({exc}); checking"
            )
        else:
            logger.info(
                f"ℹ️ channel profile {profile.id} restart: {mode} sent to "
                f"{sid[:8]}"
            )
        try:
            gone = await _wait_until_gone(port, sid, wait_s)
        except session_client.SessionHostError as exc:
            logger.warning(
                f"⚠️ channel profile {profile.id} restart: session-host "
                f"stopped answering while {sid[:8]} was stopping ({exc}); "
                "not starting"
            )
            raise HTTPException(
                status_code=502,
                detail=f"{failed} The session-host stopped answering ({exc}).",
            )
        if gone:
            logger.info(
                f"ℹ️ channel profile {profile.id} restart: {sid[:8]} confirmed "
                f"gone after {time.monotonic() - started:.1f}s"
            )
            return
        if mode == "quit":
            logger.info(
                f"ℹ️ channel profile {profile.id} restart: {sid[:8]} still "
                f"alive after {wait_s:.0f}s; kill escalated"
            )
    logger.warning(
        f"⚠️ channel profile {profile.id} restart: {sid[:8]} still alive "
        "after the kill; not starting"
    )
    raise HTTPException(
        status_code=502, detail=f"{failed} It is still running after a kill."
    )


# --- boot-time autostart (#1542) -----------------------------------------
# A reboot (a forced Windows update) kills every channel session, and the
# launcher comes back at log-on without them. With the setting on, the
# lifespan starts each ready profile once. The session-host may still be
# booting when the webapp is, so the start waits for it — bounded, logged.
AUTOSTART_HOST_WAIT_SECONDS = 180.0
AUTOSTART_HOST_POLL_SECONDS = 3.0


async def _wait_for_session_host(cfg: WebappConfig) -> bool:
    """Poll the session-host until it answers, at most
    ``AUTOSTART_HOST_WAIT_SECONDS``. ``False`` means it never did."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + AUTOSTART_HOST_WAIT_SECONDS
    while True:
        try:
            await asyncio.to_thread(
                session_client.list_sessions, cfg.session_host_port
            )
            return True
        except session_client.SessionHostError as exc:
            if loop.time() >= deadline:
                logger.warning(
                    f"⚠️ Telegram autostart: session-host still unreachable "
                    f"after {AUTOSTART_HOST_WAIT_SECONDS:.0f}s ({exc}); "
                    "no channel session started"
                )
                return False
        await asyncio.sleep(AUTOSTART_HOST_POLL_SECONDS)


def _autostart_blocker(
    cfg: WebappConfig, installed_plugins_path: Optional[Path]
) -> Optional[str]:
    """Why no profile can start at all, or ``None`` when the shared checks
    pass. The plugin record's unknown (``None``) blocks too: a start the
    launcher could not confirm would only fail inside the session."""
    life_os_dir = Path(cfg.life_os_dir)
    if not life_os_dir.is_dir():
        return "the life-os folder was not found"
    if not bun_available():
        return "Bun is not on PATH"
    plugin = plugin_installed_for(life_os_dir, installed_plugins_path)
    if plugin is None:
        return "the Telegram plugin's install record could not be read"
    if not plugin:
        return "the Telegram plugin is not installed for life-os"
    return None


async def autostart_channel_sessions(
    cfg: WebappConfig,
    *,
    profiles_path: Optional[Path] = None,
    installed_plugins_path: Optional[Path] = None,
) -> List[str]:
    """Start every ready profile that has no live session; return the ids
    started. Never raises: each skip is one info line naming why, and one
    profile failing never stops the others."""
    started: List[str] = []
    try:
        profiles, problems = load_channel_profiles(profiles_path)
        for problem in problems:
            logger.info(f"ℹ️ Telegram autostart: profile file: {problem}")
        if not profiles:
            logger.info("ℹ️ Telegram autostart: no channel profiles to start")
            return started
        blocker = _autostart_blocker(cfg, installed_plugins_path)
        if blocker:
            logger.info(
                f"ℹ️ Telegram autostart: skipped all {len(profiles)} "
                f"profile(s): {blocker}"
            )
            return started
        skill_ids = {s.id for s in scan_skills(Path(cfg.life_os_dir))}
        ready: List[ChannelProfile] = []
        for profile in profiles:
            if profile.skill not in skill_ids:
                why = f"its skill {profile.skill!r} was not found"
            elif not env_file_present(profile):
                why = "its token file (.env) is missing"
            else:
                ready.append(profile)
                continue
            logger.info(
                f"ℹ️ Telegram autostart: profile {profile.id} skipped: {why}"
            )
        if not ready:
            return started
        logger.info(
            f"ℹ️ Telegram autostart: waiting for the session-host to start "
            f"{len(ready)} profile(s)"
        )
        if not await _wait_for_session_host(cfg):
            return started
    except Exception:  # noqa: BLE001 — never take the lifespan down
        logger.exception("❌ Telegram autostart failed before any launch")
        return started

    for profile in ready:
        try:
            result = await start_channel_session(
                cfg, profile, model=cfg.channel_model, resume=False, body={},
                request=None,
            )
        except HTTPException as exc:
            # 409 is the already-running refusal: that bot is fine as it is.
            logger.info(
                f"ℹ️ Telegram autostart: profile {profile.id} skipped: "
                f"{exc.detail}"
            )
            continue
        except Exception:  # noqa: BLE001 — one profile never stops the others
            logger.exception(
                f"❌ Telegram autostart: profile {profile.id} failed to start"
            )
            continue
        sid = str((result.get("session") or {}).get("session_id") or "")
        logger.info(
            f"✅ Telegram autostart: profile {profile.id} started in "
            f"session {sid[:8]}"
        )
        started.append(profile.id)
    return started
