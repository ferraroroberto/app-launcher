"""Skill resolution + the spawn tail shared by the Life OS launch routes.

Split off ``app/webapp/routers/life_os.py`` (issue #884, a `/codebase-audit`
maintainability finding) alongside :mod:`app.webapp.routers.life_os_conversations`
and :mod:`app.webapp.routers.life_os_files`. The skill-launch, recap-launch and
conversation-launch routes all resolve a skill, a provider/model choice, and
then run the same spawn + audit + PC-mirror tail; those three live here so that
``life_os.py`` (which mounts the conversations router and delegates a cached
client's targeted resume into it) and ``life_os_conversations.py`` (which
launches through this tail) never import each other — the role
:mod:`app.webapp.routers.board_spawn` plays for ``board.py`` and
``board_chief.py``. Not a router: nothing here is mounted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from fastapi import HTTPException, Request

from src import audit
from src.model_catalog import (
    CLAUDE_MODEL_SPECS,
    CODEX_MODEL_SPECS,
    available_values,
)
from src.launcher import open_local_terminal_window, spawn_claude_session
from src.scanner import Skill, scan_skills
from src.webapp_config import WebappConfig

from app.webapp.routers._helpers import (
    audit_session_start_and_maybe_mirror,
    safe_int,
    spawn_launcher_session,
)

# The launch choice comes from the same provider-qualified catalog used by
# Coding and Board (#845). Legacy unqualified Claude values remain accepted so
# a cached pre-#845 browser can still launch safely after the webapp restarts.
_LAUNCH_MODELS = {
    "claude": available_values(CLAUDE_MODEL_SPECS),
    "codex": available_values(CODEX_MODEL_SPECS),
}


def _resolve_launch_choice(body: Dict[str, Any]) -> tuple[str, str]:
    """Resolve the per-launch provider and model from the request body.

    The tab now sends an explicit ``model`` (#540, board parity). Older
    callers sent an ``opus`` bool (on → opus, off → sonnet); accept it as a
    fallback so an out-of-date cached client still launches correctly.
    """
    raw = body.get("model")
    if raw is not None:
        choice = str(raw).strip().lower()
        if ":" in choice:
            agent, model = choice.split(":", 1)
        else:
            agent, model = "claude", choice
        if agent not in _LAUNCH_MODELS or model not in _LAUNCH_MODELS[agent]:
            raise HTTPException(
                status_code=400,
                detail=f"unsupported Life OS model choice: {choice!r}",
            )
        return agent, model
    return "claude", "opus" if bool(body.get("opus", False)) else "sonnet"


def _resolve_skill(cfg: WebappConfig, skill_id: str) -> Skill:
    """Find a skill by folder id from the live scan, or 404.

    The launch slash-command is re-derived here from the validated scan
    (``skill.command``) — never taken from the URL — so a crafted path
    param can't reach the command line.
    """
    life_os_dir = Path(cfg.life_os_dir)
    skill = next(
        (s for s in scan_skills(life_os_dir) if s.id == skill_id), None
    )
    if skill is None:
        raise HTTPException(status_code=404, detail=f"unknown skill: {skill_id}")
    return skill


async def _spawn_skill_session(
    cfg: WebappConfig,
    request: Request,
    life_os_dir: Path,
    *,
    flags: str,
    name: str,
    kind: str,
    agent: str,
    model: str,
    resume: bool,
    audit_skill: str,
    body: Dict[str, Any],
    resume_sid: str = "",
) -> Dict[str, Any]:
    """Spawn a Claude or Codex session in life-os and shape the reply.

    The shared tail of the skill-launch and recap-launch routes: each has
    already resolved provider-specific flags and the session kind; this runs
    the spawn + audit + optional PC mirror identically and returns the common
    response fields. The caller prepends its own ``launched`` id.
    """
    # The phone passes its real terminal size (issue #374): a skill streams
    # output the moment the PTY spawns, so spawning at the legacy 40×120
    # poured 120-col text that re-wrapped into garble when the overlay's
    # first fit() shrank the PTY to phone width. Same contract as the
    # Coding-tab launch route (issue #126); ignored for kind="remote".
    rows = safe_int(body, "rows", 40)
    cols = safe_int(body, "cols", 120)
    session, sid = await spawn_launcher_session(
        spawn_claude_session, cfg,
        project_dir=life_os_dir, name=name, flags=flags, agent=agent,
        rows=rows, cols=cols, kind=kind,
    )
    # The shared audit+mirror tail (#1003). This used to be a second,
    # parallel copy of _helpers.audit_session_start_and_maybe_mirror,
    # differing only in the remote-kind event name, the resume_sid field and
    # the PTY-only mirror guard — all three are parameters now, so one
    # workflow is maintained in one place.
    await audit_session_start_and_maybe_mirror(
        cfg, request, body,
        sid=sid, agent=agent, name=name, project=str(life_os_dir),
        skill=audit_skill, resume=resume, kind=kind, resume_sid=resume_sid,
        audit_mod=audit, mirror_fn=open_local_terminal_window,
    )

    return {
        "name": name,
        "agent": agent,
        "mode": kind,
        "model": model,
        "resume": resume,
        "resume_sid": resume_sid,
        "session": session,
    }
