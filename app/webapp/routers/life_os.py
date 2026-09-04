"""Life OS tab — one-tap skill launch + read-only private-content browser.

The Life OS tab (issue #102) is ~80% a clone of the Coding tab,
specialised to the skills in the sibling ``life-os`` repo:

    GET  /api/life-os/skills                  → list skills (public, token-gated)
    POST /api/life-os/skills/{id}/launch      → spawn a claude session that
                                                 auto-invokes /<skill> (public)
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

The content endpoints surface private, gitignored knowledge
(``context/`` ``memory/`` ``examples/`` ``conversations/`` + the shared
``identity/``). They are gated like the live terminal — refused over the
Cloudflare tunnel, Tailscale-only, passkey-required (see
``app/webapp/middleware.py``) — and the file-content endpoint is
**path-jailed** to ``life_os_dir`` (the jail is the whole security story
for an endpoint that reads arbitrary files under a root).

Conversations (issue #727) are the same private content one level up: the
capture/index pipeline in life-os (life-os#68, fleet-config#586) writes a
digested ``conversations/index.json`` per skill and keeps a cross-skill FTS5
database, and this router surfaces both so the phone can *find* one
conversation and reopen exactly it — rather than scrolling Claude's native
session picker. Neither endpoint owns any of that logic: the list is a JSON
read, and the search shells out to fleet-config's own ``conversation_search``
CLI. Both degrade to ``available: false`` when the pipeline hasn't produced
its artefacts yet, so the tab is honest rather than broken on a fresh machine.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from src import audit
from src.launch_flags import build_claude_flags, build_codex_flags, build_resume_flags
from src.model_catalog import (
    CLAUDE_MODEL_SPECS,
    CODEX_MODEL_SPECS,
    available_values,
)
from src.launcher import open_local_terminal_window, spawn_claude_session
from src.scanner import Skill, scan_skills, skills_dir_for
from src.subprocess_flags import NO_WINDOW
from src.webapp_config import WebappConfig

from app.webapp.routers._helpers import (
    audit_off_loop,
    client_ip,
    maybe_json,
    mirror_url,
    safe_int,
    should_mirror_to_pc,
    spawn_session_or_400,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# Files surfaced by the content browser — text-ish only; everything else
# (images, binaries) is skipped. No suffix is treated as text too (some
# notes files carry none).
_TEXT_SUFFIXES = frozenset(
    {"", ".md", ".markdown", ".txt", ".json", ".yaml", ".yml", ".csv", ".log"}
)
# Cap a single file read so a stray huge file can't blow up the phone.
_MAX_FILE_BYTES = 256 * 1024
# Directory names never walked for the browser (VCS / caches).
_BROWSE_SKIP_DIRS = frozenset({".git", "__pycache__", ".venv", "node_modules"})

# --- conversations (issue #727) ----------------------------------------
# The machine-readable twin of `conversations/index.md`, written by
# fleet-config's conversation_index hook (life-os#68): one digested entry per
# conversation, newest-first, carrying the full resumable session id.
_CONVERSATIONS_DIR = "conversations"
_CONVERSATIONS_INDEX = "index.json"
# The cross-skill ranked search CLI lives in the fleet-config checkout
# (`claude_config_dir`) — the same repo the Board already shells into for
# `chief_managed.py`. Resolved per request so a Settings change takes effect
# without a restart.
_SEARCH_SCRIPT_REL = ("hooks", "conversation_search.py")
_SEARCH_TIMEOUT_S = 15
# A query long enough to be a paste accident rather than a search; the CLI is
# invoked with an argv list (never a shell), so this is a sanity cap, not the
# injection guard.
_MAX_QUERY_CHARS = 200
_SEARCH_LIMIT_DEFAULT = 20
_SEARCH_LIMIT_MAX = 100

# A resumable session id, validated strictly because it reaches claude's
# command line. Same by-construction stance as the skill slug: the value is
# either a canonical UUID or the request is refused — never sanitised into
# something "close enough".
_SESSION_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$"
)

# --- weekly recap (issue #167) -----------------------------------------
# The recap is the ``_recap`` infra skill; it is underscore-prefixed, so
# ``scan_skills`` deliberately skips it and the normal skill-launch route
# can't reach it. The Life OS tab surfaces it as a dedicated "Weekly recap"
# tile instead: a staleness badge driven by the ledger's mtime, and a launch
# that invokes ``/weekly-recap`` (the interactive review). A safe literal slug
# (validated by construction — matches ``scanner._SKILL_SLUG_RE``); if the
# skill is ever renamed the tile 404s visibly rather than launching the wrong
# thing.
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


# ------------------------------------------------------------- path jail


def resolve_within(root: Path, rel: str) -> Optional[Path]:
    """Resolve ``rel`` under ``root``, or ``None`` if it escapes the root.

    The whole security story for the file-content endpoint: reject any
    absolute path, drive-letter, or ``..`` traversal that would resolve
    outside ``root``. Returns the resolved, existing file path on success.
    """
    if not rel:
        return None
    try:
        root_resolved = root.resolve()
        candidate = (root_resolved / rel).resolve()
    except (OSError, ValueError):
        return None
    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        return None
    return candidate


# --------------------------------------------------------------- helpers


def _skill_to_api(skill: Skill, life_os_root: Path) -> Dict[str, Any]:
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
    }


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
    session = await spawn_session_or_400(
        spawn_claude_session,
        life_os_dir,
        name,
        flags,
        cfg.session_host_port,
        kind,
        agent,
        rows,
        cols,
        history_lines=cfg.terminal_history_lines,
    )

    sid = str(session.get("session_id") or "")
    event = "remote_launch" if kind == "remote" else "session_start"
    await audit_off_loop(
        audit.audit_event,
        event,
        session=sid,
        agent=agent,
        skill=audit_skill,
        name=name,
        project=str(life_os_dir),
        resume=resume,
        # Which conversation was reattached (#727) — "" for a fresh launch or
        # the native picker, where no id was chosen up front.
        resume_sid=resume_sid,
        client=client_ip(request),
    )
    await audit_off_loop(
        audit.session_log,
        sid, "start", agent=agent, skill=audit_skill, name=name,
        project=str(life_os_dir),
    )

    # Mirror full-control sessions into a dedicated PC terminal window —
    # identical to the Coding tab (issue #241, widened by #609): the default
    # for every caller, unless the launcher explicitly says it's rendering
    # in-page itself (see should_mirror_to_pc).
    if kind == "pty" and should_mirror_to_pc(
        cfg.claude_show_local_window, request, body
    ):
        asyncio.create_task(
            asyncio.to_thread(
                open_local_terminal_window, mirror_url(request, cfg, sid), sid
            )
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
    skills = [
        _skill_to_api(s, life_os_root) for s in scan_skills(life_os_dir)
    ] if available else []
    return {
        "skills": skills,
        "life_os_dir": cfg.life_os_dir,
        "available": available,
    }


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

    **Targeted resume** (issue #727) skips the picker entirely: a
    ``resume_sid`` in the body reattaches to that exact conversation via
    ``--resume <id>``, the non-interactive path :func:`build_resume_flags`
    already implements for the fleet chief (issue #633). It is what the
    Conversations view's ↺ posts, and it is orthogonal to Detached in the
    same way. The id is validated as a canonical UUID before it goes
    anywhere near a command line; a bare ``resume: true`` (the picker) is
    unchanged.
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
    resume = bool(resume_sid) or bool(body.get("resume", False))
    if resume_sid and agent != "claude":
        raise HTTPException(
            status_code=400,
            detail="stored Life OS conversation ids can only resume with Claude",
        )

    # Model override is per-launch (the tab's model combo, #540); the rest of
    # the flags (effort / permission / verbose / debug) come from the shared
    # Coding options. The bare /<skill> is appended as claude's positional
    # prompt — skill.command is a validated slug, so no shell-quoting is needed.
    # On Resume we drop the /<skill> prompt and invoke the native picker with
    # the positional /resume command. Starting the interactive session through
    # build_claude_flags first is intentional: Claude Code can carry
    # `--resume --remote-control` in its process command without activating
    # Remote Control for the selected conversation (issue #526).
    # A targeted resume (#727) pins that same resume path to one conversation
    # id instead of rendering the picker — same builder the fleet chief uses
    # (#633), so there is one place where `--resume <id>` is composed.
    if resume_sid:
        flags = build_resume_flags(
            cfg, "claude", model_override=model, session_id=resume_sid
        )
    elif resume and agent == "claude":
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


# ------------------------------------------------- conversations (issue #727)


def _capture_rel(life_os_dir: Path, capture: Path) -> str:
    """``capture`` relative to ``life_os_dir``, or ``""`` when it escapes it.

    The shape ``/api/life-os/file`` accepts, so a conversation row can open
    its own raw capture through the existing (path-jailed) viewer instead of
    a second file-read surface. Deliberately **not** ``_rel_to_root``'s
    fall-back-to-basename behaviour: a capture that resolves outside the root
    has no viewable path at all, and saying ``""`` lets the UI hide the
    control rather than offer one that 404s.
    """
    try:
        return str(capture.resolve().relative_to(life_os_dir.resolve()))
    except (OSError, ValueError):
        return ""


def _conversation_api(
    row: Dict[str, Any],
    life_os_dir: Path,
    capture: Path,
    *,
    default_skill: str,
) -> Dict[str, Any]:
    """API shape for one conversation row (index entry or search hit).

    ``resumable`` is computed here rather than trusted from the source, and
    by exactly the rule :func:`launch_skill` enforces — a canonical session
    id belonging to a claude conversation. That keeps the two in lockstep:
    the UI can never enable a ↺ the launch route would reject with a 400.
    Roughly a quarter of the existing archive predates the stored session id
    and is legitimately unresumable, so this is a common state, not an edge
    case — the client shows it, it never silently disappears.
    """
    sid = str(row.get("sid") or "")
    agent = str(row.get("agent") or "")
    return {
        "skill": str(row.get("skill") or default_skill),
        "file": str(row.get("file") or ""),
        "path": _capture_rel(life_os_dir, capture),
        "date": str(row.get("date") or ""),
        "slug": str(row.get("slug") or ""),
        "turns": row.get("turns") or 0,
        "sid": sid,
        "agent": agent,
        "topic": str(row.get("topic") or ""),
        "decisions": str(row.get("decisions") or ""),
        "open_loops": str(row.get("open_loops") or ""),
        "resumable": agent == "claude" and bool(_SESSION_ID_RE.match(sid)),
    }


def _read_conversation_index(path: Path) -> Optional[List[Dict[str, Any]]]:
    """Parse a skill's ``conversations/index.json``, or ``None``.

    ``None`` means "no usable index" — absent (the indexer hasn't run for
    this skill yet), unreadable, or not the list of objects it should be.
    Every one of those is an honest ``available: false`` to the caller, never
    a 500: the launcher does not own this file and must not fail when the
    pipeline that writes it hasn't caught up.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("⚠️ unreadable conversation index: %s", path)
        return None
    if not isinstance(data, list):
        logger.warning("⚠️ conversation index is not a list: %s", path)
        return None
    return [row for row in data if isinstance(row, dict) and row.get("file")]


@router.get("/api/life-os/skills/{skill_id}/conversations")
async def list_skill_conversations(skill_id: str, request: Request) -> Dict[str, Any]:
    """One skill's digested conversation index (Tailscale + passkey, gated upstream).

    Reads ``<skill>/conversations/index.json`` — the machine-readable twin of
    ``index.md``, written by life-os's own capture/index pipeline
    (life-os#68) — and returns its entries newest-first. Each carries the
    digest (topic / decisions / open loops), the stored session id, and a
    ``path`` to the raw capture for the existing file viewer.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    life_os_dir = Path(cfg.life_os_dir)
    skill = _resolve_skill(cfg, skill_id)

    conv_dir = skill.skill_dir / _CONVERSATIONS_DIR
    rows = _read_conversation_index(conv_dir / _CONVERSATIONS_INDEX)
    if rows is None:
        return {"skill": skill.id, "available": False, "conversations": []}

    conversations = [
        _conversation_api(
            row, life_os_dir, conv_dir / str(row["file"]), default_skill=skill.id
        )
        for row in rows
    ]
    # The indexer already writes newest-first; re-sorting on the date-stamped
    # filename makes that a property of this endpoint rather than a hope.
    conversations.sort(key=lambda c: c["file"], reverse=True)
    return {
        "skill": skill.id,
        "available": True,
        "conversations": conversations,
    }


def _search_unavailable(reason: str) -> Dict[str, Any]:
    """The degraded-but-honest search reply.

    ``reason`` is a short, already-sanitised sentence for the phone — never
    an exception string, a path, or a stderr dump (those go to the log). The
    UI renders it as "search unavailable", not an error toast.
    """
    return {"available": False, "reason": reason, "results": []}


def _search_cli(cfg: WebappConfig) -> Optional[List[str]]:
    """``[python, script]`` for fleet-config's search CLI, or ``None``.

    Resolved per request from ``claude_config_dir`` (the fleet-config
    checkout the Board already shells into) so pointing Settings at a
    different checkout takes effect without a restart. ``None`` when either
    half is missing — a machine without fleet-config still gets a working
    Life OS tab, minus search.
    """
    root = Path(cfg.claude_config_dir)
    script = root.joinpath(*_SEARCH_SCRIPT_REL)
    if not script.is_file():
        return None
    for rel in ((".venv", "Scripts", "python.exe"), (".venv", "bin", "python")):
        python = root.joinpath(*rel)
        if python.is_file():
            return [str(python), str(script)]
    return None


def _search_limit(raw: Optional[str]) -> int:
    """Clamp the caller's ``limit`` into a sane range."""
    try:
        limit = int(raw) if raw else _SEARCH_LIMIT_DEFAULT
    except (TypeError, ValueError):
        return _SEARCH_LIMIT_DEFAULT
    return max(1, min(limit, _SEARCH_LIMIT_MAX))


@router.get("/api/life-os/conversations/search")
async def search_conversations(request: Request) -> Dict[str, Any]:
    """Ranked conversation search across every skill (Tailscale + passkey).

    Shells out to fleet-config's ``conversation_search`` CLI — the launcher
    owns none of the ranking: that is an FTS5 index over the digests *and*
    the full capture text, so an offhand detail no digest mentions still
    finds the right conversation. ``--cwd`` (not ``--project``) resolves the
    project from the configured ``life_os_dir``, so a non-default checkout
    still works.

    Every failure mode — no fleet-config, no database yet, a non-zero exit, a
    timeout, unreadable output — degrades to ``available: false`` with a
    short reason. This endpoint never 500s and never surfaces infrastructure
    detail to the phone; the detail goes to the log.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    life_os_dir = Path(cfg.life_os_dir)

    query = (request.query_params.get("q") or "").strip()
    skill = (request.query_params.get("skill") or "").strip()
    limit = _search_limit(request.query_params.get("limit"))
    # An empty box is not a failure and not a search — answer it without
    # spawning anything, so typing-then-clearing costs nothing.
    if not query:
        return {"available": True, "query": "", "skill": skill, "results": []}
    if len(query) > _MAX_QUERY_CHARS:
        raise HTTPException(status_code=400, detail="query too long")

    cli = _search_cli(cfg)
    if cli is None:
        return _search_unavailable("conversation search is not installed")

    argv = [
        *cli, "--cwd", str(life_os_dir), "--query", query,
        "--limit", str(limit), "--json",
    ]
    if skill:
        argv.extend(["--skill", skill])
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_SEARCH_TIMEOUT_S,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("⚠️ conversation search did not run: %s", exc)
        return _search_unavailable("search is not responding")
    if proc.returncode != 0:
        logger.warning(
            "⚠️ conversation search exited %s: %s",
            proc.returncode, (proc.stderr or "").strip()[:400],
        )
        return _search_unavailable("no conversation index has been built yet")
    try:
        rows = json.loads(proc.stdout or "[]")
    except ValueError:
        logger.warning("⚠️ conversation search returned unparseable JSON")
        return _search_unavailable("search returned an unreadable result")
    if not isinstance(rows, list):
        return _search_unavailable("search returned an unreadable result")

    results = [
        _conversation_api(
            row, life_os_dir, Path(str(row.get("path") or "")), default_skill=""
        )
        for row in rows
        if isinstance(row, dict)
    ]
    # Audited like every other private-content read, but the query text is
    # deliberately not recorded: it is the user's own words about their own
    # life, and the hit count is all an audit trail needs to be useful here.
    await audit_off_loop(
        audit.audit_event,
        "lifeos_search", skill=skill, hits=len(results), client=client_ip(request)
    )
    return {
        "available": True,
        "query": query,
        "skill": skill,
        "results": results,
    }


@router.get("/api/life-os/file")
async def get_file(request: Request) -> Dict[str, Any]:
    """Return a single file's text content (Tailscale + passkey, path-jailed).

    ``path`` is relative to ``life_os_dir``; anything escaping that root
    (absolute paths, ``..`` traversal) is rejected — the jail is the whole
    security story here. Non-text / oversized files are refused.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    rel = request.query_params.get("path", "")
    resolved = resolve_within(Path(cfg.life_os_dir), rel)
    if resolved is None:
        raise HTTPException(status_code=400, detail="path escapes life_os_dir")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    if resolved.suffix.lower() not in _TEXT_SUFFIXES:
        raise HTTPException(status_code=415, detail="not a text file")
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    truncated = len(raw) > _MAX_FILE_BYTES
    content = raw[:_MAX_FILE_BYTES].decode("utf-8", errors="replace")
    await audit_off_loop(
        audit.audit_event,
        "lifeos_read", path=rel, bytes=len(raw), client=client_ip(request)
    )
    return {"path": rel, "name": resolved.name, "content": content, "truncated": truncated}


@router.delete("/api/life-os/file")
async def delete_file(request: Request) -> Dict[str, Any]:
    """Delete a single **conversation log** (Tailscale + passkey, path-jailed).

    Deliberately narrow: only files under a skill's ``conversations/``
    directory can be deleted — never source files (``SKILL.md``,
    ``description.md``, …) or any other private dir. The path is jailed to
    ``life_os_dir`` first, then required to live under
    ``.claude/skills/<skill>/conversations/``. Used by the browser's
    edit-mode 🗑️ to declutter trial-run logs.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    rel = request.query_params.get("path", "")
    resolved = resolve_within(Path(cfg.life_os_dir), rel)
    if resolved is None:
        raise HTTPException(status_code=400, detail="path escapes life_os_dir")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    if not _is_conversation_file(Path(cfg.life_os_dir), resolved):
        raise HTTPException(
            status_code=403,
            detail="only conversation logs can be deleted",
        )
    try:
        resolved.unlink()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await audit_off_loop(
        audit.audit_event, "lifeos_delete", path=rel, client=client_ip(request)
    )
    return {"deleted": rel}


# Date-stamped prefix (YYYY-MM-DD-HHMM-) a rename preserves — only the slug
# after it changes (mirrors fleet-config's conversation_capture.py naming).
_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}-\d{4}-)")


def _sanitize_slug(raw: str) -> str:
    """Lower-case, collapse non-alphanumeric runs to single dashes, trim.

    Server-side mirror of the client slugify — the real guard: even a
    crafted ``slug`` can only ever become ``[a-z0-9-]`` chars, so it can't
    carry a path separator or ``..`` into the new filename.
    """
    return re.sub(r"[^a-z0-9]+", "-", str(raw).strip().lower()).strip("-")


def _renamed(old_name: str, slug: str) -> str:
    """New filename: keep the date prefix + extension, swap in ``slug``."""
    stem = Path(old_name).stem
    ext = Path(old_name).suffix
    match = _DATE_PREFIX_RE.match(stem)
    prefix = match.group(1) if match else ""
    return f"{prefix}{slug}{ext}"


def _rel_to_root(life_os_dir: Path, path: Path) -> str:
    """Path relative to ``life_os_dir`` (the shape the file endpoints use)."""
    try:
        return str(path.resolve().relative_to(life_os_dir.resolve()))
    except (OSError, ValueError):
        return path.name


@router.post("/api/life-os/file/rename")
async def rename_file(request: Request) -> Dict[str, Any]:
    """Rename a single **conversation log**, keeping its date prefix.

    Body: ``{"path": <rel>, "slug": <new words>}`` (Tailscale + passkey,
    path-jailed). Same narrow guard as delete — only files under a skill's
    ``conversations/`` (never source files or the ``.gitkeep`` placeholder).
    The new name keeps the existing ``YYYY-MM-DD-HHMM-`` prefix and
    extension; only the slug after it is replaced (sanitised server-side, so
    a crafted slug can't traverse out). Refuses to clobber an existing file.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    body = await maybe_json(request)
    rel = str(body.get("path") or "")
    slug = _sanitize_slug(body.get("slug") or "")

    resolved = resolve_within(Path(cfg.life_os_dir), rel)
    if resolved is None:
        raise HTTPException(status_code=400, detail="path escapes life_os_dir")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    if not _is_conversation_file(Path(cfg.life_os_dir), resolved):
        raise HTTPException(
            status_code=403, detail="only conversation logs can be renamed"
        )
    if not slug:
        raise HTTPException(status_code=400, detail="name cannot be empty")

    target = resolved.with_name(_renamed(resolved.name, slug))
    new_rel = _rel_to_root(Path(cfg.life_os_dir), target)
    if target == resolved:
        # Same slug — a no-op; report success without touching disk.
        return {"renamed": rel, "to": new_rel, "name": target.name}
    if target.exists():
        raise HTTPException(
            status_code=409, detail="a file with that name already exists"
        )
    try:
        resolved.rename(target)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await audit_off_loop(
        audit.audit_event,
        "lifeos_rename", path=rel, to=new_rel, client=client_ip(request)
    )
    return {"renamed": rel, "to": new_rel, "name": target.name}


def _is_conversation_file(life_os_dir: Path, resolved: Path) -> bool:
    """True only for a real log under ``.claude/skills/<skill>/conversations/``.

    The delete/rename guard — anything else (source files, other private
    dirs, files outside any skill) is rejected. The ``.gitkeep`` placeholder
    that keeps an empty ``conversations/`` tracked in git is explicitly
    excluded: deleting or renaming it would untrack the directory.
    """
    if resolved.name == ".gitkeep":
        return False
    try:
        skills_root = skills_dir_for(life_os_dir).resolve()
        parts = resolved.relative_to(skills_root).parts
    except (OSError, ValueError):
        return False
    # parts == (<skill>, "conversations", <file…>)
    return len(parts) >= 3 and parts[1] == "conversations"


# --------------------------------------------------------------- walk


def _walk_files(
    root: Path, life_os_root: Path, category: Optional[str]
) -> List[Dict[str, str]]:
    """List text files under ``root`` as ``{path, name, category}`` dicts.

    ``path`` is relative to ``life_os_root`` (what the file endpoint
    accepts); ``name`` is a readable row label; ``category`` is the
    caller's label, or — when ``None`` — the first path component under
    ``root`` (so a skill's ``memory/observations.md`` lands under category
    ``memory`` and a top-level ``SKILL.md`` under ``skill``). When the
    category is derived from that leading directory, ``name`` drops it —
    the section header already shows it, so repeating it in the row just
    wastes horizontal space (#118). Sorted by category then path.
    """
    if not root.is_dir():
        return []
    out: List[Dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _BROWSE_SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            rel_root = path.resolve().relative_to(life_os_root)
            rel_name = path.relative_to(root)
        except (OSError, ValueError):
            continue
        if category is not None:
            cat = category
            name = str(rel_name)
        else:
            parts = rel_name.parts
            if len(parts) > 1:
                # Leading dir becomes the category — drop it from the label
                # so the row doesn't echo its own section header (#118).
                cat = parts[0]
                name = str(Path(*parts[1:]))
            else:
                cat = "skill"
                name = str(rel_name)
        out.append(
            {"path": str(rel_root), "name": name, "category": cat}
        )
    out.sort(key=lambda f: (f["category"], f["path"]))
    return out
