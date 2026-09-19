"""Life OS tab — one-tap skill launch + read-only private-content browser.

The Life OS tab (issue #102) is ~80% a clone of the Coding tab,
specialised to the skills in the sibling ``life-os`` repo:

    GET  /api/life-os/skills                  → list skills (public, token-gated)
    POST /api/life-os/skills/{id}/launch      → spawn a skill session (token)
    POST /api/life-os/skills/{id}/conversations/launch → verified source resume
                                                 or new handoff (Tailscale + passkey)
    GET  /api/life-os/skills/{id}/files        → file tree   (Tailscale + passkey)
    GET  /api/life-os/skills/{id}/conversations → digested conversation index
                                                 (Tailscale + passkey)
    GET  /api/life-os/conversations/search     → ranked cross-skill search
                                                 (Tailscale + passkey)
    GET  /api/life-os/file?path=…              → file content (Tailscale + passkey)

Launch reuses the Coding tab's session-host / ConPTY machinery wholesale
(:func:`src.launcher.spawn_claude_session`). The cwd is always ``life_os_dir``
so project skills resolve. Claude receives the native bare ``/<skill>``
slash-command; Codex receives a server-authored prompt pointing at that same
validated skill's ``SKILL.md``. No caller-supplied free text is interpolated
into either launch.

Split by concern (issue #884, a `/codebase-audit` maintainability finding),
the way ``board.py`` was under #691. This module is the launch surface — the
skill list, the weekly-recap tile, skill launch, and a skill's file tree. The
conversation history (index, search, targeted relaunch) lives in
:mod:`app.webapp.routers.life_os_conversations` and the path-jailed content
browser (read / delete / rename) in :mod:`app.webapp.routers.life_os_files`,
both mounted here via ``include_router`` so ``app/webapp/server.py`` still
registers one ``life_os.router``. Skill resolution and the spawn tail that
this module and the conversations router share live in
:mod:`app.webapp.routers.life_os_spawn`, which neither imports the other
through.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from src.launch_flags import build_claude_flags, build_codex_flags, build_resume_flags
from src.scanner import Skill, scan_skills, skills_dir_for
from src.webapp_config import WebappConfig, update_webapp_config

from app.webapp.routers import life_os_conversations, life_os_files
from app.webapp.routers._helpers import maybe_json
from app.webapp.routers.life_os_conversations import (
    _SESSION_ID_RE,
    _launch_conversation,
    _require_history_access,
)
from app.webapp.routers.life_os_files import _walk_files
from app.webapp.routers.life_os_spawn import (
    _resolve_launch_choice,
    _resolve_skill,
    _spawn_skill_session,
)

router = APIRouter()
router.include_router(life_os_conversations.router)
router.include_router(life_os_files.router)

# --- weekly recap (issue #167) -----------------------------------------
# The recap is the ``_recap`` infra skill; it is underscore-prefixed, so
# ``scan_skills`` deliberately skips it and the normal skill-launch route
# can't reach it. The Life OS tab surfaces it as a dedicated "Weekly recap"
# tile instead: a staleness badge driven by the ledger's mtime, and a launch
# that invokes ``/weekly-recap`` (the interactive review). A safe literal slug
# (validated by construction — matches ``scanner._SKILL_SLUG_RE``); if the
# skill is ever renamed the tile 404s visibly rather than launching the wrong
# thing.


def _codex_skill_prompt(skill_path: str, skill_name: str) -> str:
    """Return a quoted initial prompt that makes a Claude-layout skill explicit."""
    return f'"Use the {skill_name} skill from {skill_path}/SKILL.md."'


_RECAP_COMMAND = "weekly-recap"
# The ledger is (re)written only when the user promotes a recap in review, so
# its mtime is "when the memory was last curated" — exactly the staleness clock.
_RECAP_LEDGER_REL = ".claude/skills/_recap/memory/ledger.json"
# Headless drafts awaiting review land here (gitignored on the life-os side).
_RECAP_PROPOSALS_REL = ".claude/skills/_recap/proposals"
# Staleness thresholds in days: amber past DUE, red past OVERDUE.
_RECAP_DUE_DAYS = 7
_RECAP_OVERDUE_DAYS = 14


def _recap_staleness(age_days: Optional[float]) -> str:
    """Map a ledger age in days to a badge state.

    ``never`` (no ledger yet) → ``fresh`` (≤7d) → ``due`` (>7d, amber) →
    ``overdue`` (>14d, red). The boundaries are inclusive of the lower band:
    exactly 7.0 days is still ``fresh``, just over is ``due``.
    """
    if age_days is None:
        return "never"
    if age_days > _RECAP_OVERDUE_DAYS:
        return "overdue"
    if age_days > _RECAP_DUE_DAYS:
        return "due"
    return "fresh"


# --------------------------------------------------------------- helpers


def _skill_to_api(
    skill: Skill, life_os_root: Path, favorites: frozenset = frozenset()
) -> Dict[str, Any]:
    """API shape for one skill tile."""
    skill_md = skill.skill_dir / "SKILL.md"
    skill_md_rel = None
    if skill_md.is_file():
        try:
            skill_md_rel = str(skill_md.resolve().relative_to(life_os_root))
        except (OSError, ValueError):
            skill_md_rel = None
    return {
        "id": skill.id,
        "name": skill.name,
        "command": skill.command,
        "description": skill.description,
        "skill_md": skill_md_rel,
        # Starred by the user (#1070) — the Coding tab's `is_favorite`
        # contract (#250), replicated here.
        "is_favorite": skill.id in favorites,
    }


# ----------------------------------------------------------------- routes


@router.get("/api/life-os/skills")
async def list_skills(request: Request) -> Dict[str, Any]:
    """List the life-os skills, live and alphabetical (public, token-gated).

    ``available`` is ``False`` when the skills dir doesn't exist (life-os
    not checked out, or ``life_os_dir`` mis-set) — the tab then shows
    disabled, the same way the Coding tab handles a missing projects_dir.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    life_os_dir = Path(cfg.life_os_dir)
    available = skills_dir_for(life_os_dir).is_dir()
    try:
        life_os_root = life_os_dir.resolve()
    except (OSError, ValueError):
        life_os_root = life_os_dir
    favorites = frozenset(cfg.life_os_favorites)
    skills = [
        _skill_to_api(s, life_os_root, favorites) for s in scan_skills(life_os_dir)
    ] if available else []
    return {
        "skills": skills,
        "life_os_dir": cfg.life_os_dir,
        "available": available,
    }


@router.post("/api/life-os/favorites")
async def toggle_skill_favorite(request: Request) -> Dict[str, Any]:
    """Star/unstar a Life OS skill (issue #1070).

    Body: ``{"id": "<skill-id>", "favorite": true|false}``. Deliberately the
    same shape, the same idempotence and the same persistence path as the
    Coding tab's ``POST /api/claude-code/favorites`` (#250) — starring an
    already-starred (or unstarring an absent) id is a no-op that still
    returns 200, so a double-tap from the phone cannot corrupt the list.

    The id is not validated against the scanner: a skill can be renamed or
    removed on disk between a star and the next scan, and a stale entry is
    harmless — it simply matches nothing when the list is rendered. Refusing
    it would mean a disk read on every tap for no gain.
    """
    body = await maybe_json(request)
    skill_id = str(body.get("id") or "").strip()
    if not skill_id:
        raise HTTPException(status_code=400, detail="missing skill id")
    favorite = bool(body.get("favorite"))

    cfg: WebappConfig = request.app.state.webapp_config
    # Preserve order, drop dupes — the list is the user's, kept tidy.
    favorites = [f for f in cfg.life_os_favorites if f != skill_id]
    if favorite:
        favorites.append(skill_id)

    new_cfg = update_webapp_config(life_os_favorites=favorites)
    request.app.state.webapp_config = new_cfg
    return {"ok": True, "life_os_favorites": new_cfg.life_os_favorites}


@router.get("/api/life-os/recap-status")
async def recap_status(request: Request) -> Dict[str, Any]:
    """Weekly-recap staleness for the Life OS tab tile (public, token-gated).

    Reports how long since the recap ledger was last written (the user's most
    recent promotion in review) as a badge ``staleness`` state, plus whether a
    headless draft is pending review. Read-only: one ``stat`` of the ledger + a
    glob of the proposals dir, both inside ``life_os_dir`` — no new file-read
    surface. ``available`` is ``False`` when life-os isn't checked out, so the
    tile hides, exactly like the skills list.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    life_os_dir = Path(cfg.life_os_dir)
    available = skills_dir_for(life_os_dir).is_dir()

    age_days: Optional[float] = None
    ledger_exists = False
    try:
        ledger = life_os_dir / _RECAP_LEDGER_REL
        if ledger.is_file():
            ledger_exists = True
            age_days = max(0.0, (time.time() - ledger.stat().st_mtime) / 86400.0)
    except OSError:
        pass

    proposal_name: Optional[str] = None
    try:
        pdir = life_os_dir / _RECAP_PROPOSALS_REL
        if pdir.is_dir():
            names = sorted((p.name for p in pdir.glob("*.md")), reverse=True)
            proposal_name = names[0] if names else None
    except OSError:
        pass

    return {
        "available": available,
        "ledger_exists": ledger_exists,
        "age_days": None if age_days is None else round(age_days, 1),
        "staleness": _recap_staleness(age_days),
        "proposal_pending": proposal_name is not None,
        "proposal_name": proposal_name,
    }


@router.post("/api/life-os/recap/launch")
async def launch_recap(request: Request) -> Dict[str, Any]:
    """Launch a Claude or Codex session for weekly-recap review in life-os.

    The Weekly-recap tile's 🚀 — the interactive **review** half of the recap
    (issue #167 / life-os #15). Body: ``{"mode": "pty"|"remote", "model": str}``
    (``model`` is a provider-qualified catalog value; a legacy unqualified
    Claude value or ``opus`` bool is still accepted).
    The drafting half runs headless on a schedule (the recap-draft Job), so this
    tile is review-only: no draft mode and no resume. cwd is fixed to
    ``life_os_dir``.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    life_os_dir = Path(cfg.life_os_dir)
    if not life_os_dir.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"life_os_dir does not exist: {cfg.life_os_dir}",
        )

    body = await maybe_json(request)
    mode = str(body.get("mode") or "pty").strip().lower()
    agent, model = _resolve_launch_choice(body)

    if agent == "claude":
        flags = f"{build_claude_flags(cfg, model_override=model)} /{_RECAP_COMMAND}"
    else:
        prompt = _codex_skill_prompt(".claude/skills/_recap", _RECAP_COMMAND)
        flags = f"{build_codex_flags(cfg, model_override=model)} {prompt}"
    kind = "remote" if mode == "remote" else "pty"
    result = await _spawn_skill_session(
        cfg, request, life_os_dir,
        flags=flags, name=_RECAP_COMMAND, kind=kind, agent=agent, model=model,
        resume=False,
        audit_skill="_recap", body=body,
    )
    return {"launched": _RECAP_COMMAND, **result}


@router.post("/api/life-os/skills/{skill_id}/launch")
async def launch_skill(skill_id: str, request: Request) -> Dict[str, Any]:
    """Launch a Claude or Codex session that invokes a skill in life-os.

    Body: ``{"mode": "pty"|"remote", "model": str, "resume": bool}``, where
    ``model`` is a provider-qualified catalog value (#540/#845; legacy Claude
    values remain accepted). cwd is fixed to ``life_os_dir``. Claude gets the
    native slash command; Codex gets a server-authored prompt pointing at the
    validated skill file. Both use the Coding tab's PTY/detached machinery.

    Resume (issue #151) reopens the selected provider's native session picker
    instead of invoking the skill: it **drops the skill prompt** so the user
    lands on the picker to pick up a prior conversation rather than starting
    the skill afresh. Resume is orthogonal to Detached (issue #157, matching
    the Coding tab): the requested ``mode`` still decides where the picker
    renders — a detached console window (``mode="remote"``) or a streamed PTY
    (``mode="pty"``). Resume no longer forces a PTY.

    Targeted resume goes through the same private source validation as the
    Conversations action. A cached Claude client sending only resume_sid is
    accepted only when one existing capture proves that exact Claude identity.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    life_os_dir = Path(cfg.life_os_dir)
    if not life_os_dir.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"life_os_dir does not exist: {cfg.life_os_dir}",
        )
    skill = _resolve_skill(cfg, skill_id)

    body = await maybe_json(request)
    mode = str(body.get("mode") or "pty").strip().lower()
    agent, model = _resolve_launch_choice(body)
    resume_sid = str(body.get("resume_sid") or "").strip()
    if resume_sid and not _SESSION_ID_RE.match(resume_sid):
        raise HTTPException(
            status_code=400, detail="resume_sid is not a valid session id"
        )
    if resume_sid or body.get("capture") or body.get("action"):
        _require_history_access(request)
        return await _launch_conversation(skill, request, body, legacy=True)
    resume = bool(body.get("resume", False))

    # Model override is per-launch (the tab's model combo, #540); the rest of
    # the flags (effort / permission / verbose / debug) come from the shared
    # Coding options. The bare /<skill> is appended as claude's positional
    # prompt — skill.command is a validated slug, so no shell-quoting is needed.
    # On Resume we drop the /<skill> prompt and invoke the native picker with
    # the positional /resume command. Starting the interactive session through
    # build_claude_flags first is intentional: Claude Code can carry
    # `--resume --remote-control` in its process command without activating
    # Remote Control for the selected conversation (issue #526).
    if resume and agent == "claude":
        flags = f"{build_claude_flags(cfg, model_override=model)} /resume"
    elif resume:
        flags = build_resume_flags(cfg, agent, model_override=model)
    elif agent == "claude":
        flags = (
            f"{build_claude_flags(cfg, model_override=model)} /{skill.command}"
        )
    else:
        prompt = _codex_skill_prompt(
            f".claude/skills/{skill.id}", skill.command
        )
        flags = f"{build_codex_flags(cfg, model_override=model)} {prompt}"
    name = skill.name

    # Detached and Resume are orthogonal (issue #157, matching the Coding
    # tab): the requested mode decides where the session renders — a detached
    # console (remote) or a streamed PTY — independent of resume.
    kind = "remote" if mode == "remote" else "pty"
    result = await _spawn_skill_session(
        cfg, request, life_os_dir,
        flags=flags, name=name, kind=kind, agent=agent, model=model, resume=resume,
        audit_skill=skill.id, body=body, resume_sid=resume_sid,
    )
    return {"launched": skill.id, **result}


@router.get("/api/life-os/skills/{skill_id}/files")
async def list_skill_files(skill_id: str, request: Request) -> Dict[str, Any]:
    """File tree for a skill's content (Tailscale + passkey, gated upstream).

    Returns the skill's own files (public ``SKILL.md`` / ``description.md``
    / ``maintenance.md`` and the private ``context`` / ``memory`` /
    ``examples`` / ``conversations`` subtrees) plus the shared
    ``identity/``. Each entry's ``path`` is relative to ``life_os_dir`` —
    the only thing the file-content endpoint accepts.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    life_os_dir = Path(cfg.life_os_dir)
    try:
        life_os_root = life_os_dir.resolve()
    except (OSError, ValueError):
        raise HTTPException(status_code=400, detail="life_os_dir invalid")
    skill = _resolve_skill(cfg, skill_id)

    files: List[Dict[str, str]] = []
    files.extend(_walk_files(skill.skill_dir, life_os_root, category=None))
    files.extend(
        _walk_files(life_os_dir / "identity", life_os_root, category="identity")
    )
    return {
        "skill": _skill_to_api(skill, life_os_root),
        "files": files,
    }
