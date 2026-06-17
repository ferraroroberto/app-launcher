"""Life OS tab — one-tap skill launch + read-only private-content browser.

The Life OS tab (issue #102) is ~80% a clone of the Coding tab,
specialised to the skills in the sibling ``life-os`` repo:

    GET  /api/life-os/skills                  → list skills (public, token-gated)
    POST /api/life-os/skills/{id}/launch      → spawn a claude session that
                                                 auto-invokes /<skill> (public)
    GET  /api/life-os/skills/{id}/files        → file tree   (Tailscale + passkey)
    GET  /api/life-os/file?path=…              → file content (Tailscale + passkey)

Launch reuses the Coding tab's session-host / ConPTY machinery wholesale
(:func:`src.launcher.spawn_claude_session`); only three things differ:
the cwd is always ``life_os_dir`` (so the project skills resolve), the
model is forced by the tab's ``opus`` toggle, and a bare ``/<skill>``
slash-command is passed as the positional prompt. **No** free text is
ever interpolated into the launch — the user types their input into the
live terminal once the skill reports ready.

The two content endpoints surface private, gitignored knowledge
(``context/`` ``memory/`` ``examples/`` ``conversations/`` + the shared
``identity/``). They are gated like the live terminal — refused over the
Cloudflare tunnel, Tailscale-only, passkey-required (see
``app/webapp/middleware.py``) — and the file-content endpoint is
**path-jailed** to ``life_os_dir`` (the jail is the whole security story
for an endpoint that reads arbitrary files under a root).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from src import audit, session_client
from src.launcher import open_local_terminal_window, spawn_claude_session
from src.scanner import Skill, scan_skills, skills_dir_for
from src.webapp_config import WebappConfig, build_claude_flags, build_resume_flags

from app.webapp.routers._helpers import (
    cert_present,
    client_ip,
    should_mirror_to_pc,
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

# --- weekly recap (issue #167) -----------------------------------------
# The recap is the ``_recap`` infra skill; it is underscore-prefixed, so
# ``scan_skills`` deliberately skips it and the normal skill-launch route
# can't reach it. The Life OS tab surfaces it as a dedicated "Weekly recap"
# tile instead: a staleness badge driven by the ledger's mtime, and a launch
# that invokes ``/weekly-recap`` (the interactive review). A safe literal slug
# (validated by construction — matches ``scanner._SKILL_SLUG_RE``); if the
# skill is ever renamed the tile 404s visibly rather than launching the wrong
# thing.
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
    opus: bool,
    resume: bool,
    audit_skill: str,
    body: Dict[str, Any],
) -> Dict[str, Any]:
    """Spawn a claude session in life-os, audit it, mirror to PC, shape the reply.

    The shared tail of the skill-launch and recap-launch routes: each has
    already resolved the claude ``flags`` (model + the bare ``/<command>``)
    and the session ``kind``; this runs the spawn + audit + optional PC mirror
    (issue #159) identically and returns the common response fields. The caller
    prepends its own ``launched`` id.
    """
    try:
        session = await asyncio.to_thread(
            spawn_claude_session,
            life_os_dir,
            name,
            flags,
            cfg.session_host_port,
            kind,
            "claude",
        )
    except session_client.SessionHostError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    sid = str(session.get("session_id") or "")
    event = "remote_launch" if kind == "remote" else "session_start"
    audit.audit_event(
        event,
        session=sid,
        agent="claude",
        skill=audit_skill,
        name=name,
        project=str(life_os_dir),
        resume=resume,
        client=client_ip(request),
    )
    audit.session_log(
        sid, "start", agent="claude", skill=audit_skill, name=name,
        project=str(life_os_dir),
    )

    # Mirror full-control sessions into a PC terminal window (skipped when
    # the launch came from the PC itself — loopback IP or a desktop browser
    # that already shows the terminal, issue #159) — identical to the
    # Coding tab.
    if kind == "pty" and should_mirror_to_pc(
        cfg.claude_show_local_window, request, body
    ):
        scheme = "https" if cert_present() else "http"
        pc_url = f"{scheme}://127.0.0.1:{cfg.port}/?terminal={sid}"
        asyncio.create_task(
            asyncio.to_thread(open_local_terminal_window, pc_url, sid)
        )

    return {
        "name": name,
        "agent": "claude",
        "mode": kind,
        "opus": opus,
        "resume": resume,
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
    """Launch a claude session that invokes ``/weekly-recap`` (review) in life-os.

    The Weekly-recap tile's 🚀 — the interactive **review** half of the recap
    (issue #167 / life-os #15). Body: ``{"mode": "pty"|"remote", "opus": bool}``.
    The drafting half runs headless on a schedule (the recap-draft Job), so this
    tile is review-only: no ``/weekly-recap draft`` and no resume. cwd is fixed
    to ``life_os_dir``; the positional prompt is a bare ``/weekly-recap``.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    life_os_dir = Path(cfg.life_os_dir)
    if not life_os_dir.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"life_os_dir does not exist: {cfg.life_os_dir}",
        )

    body = await request.json() if (
        request.headers.get("content-type", "").startswith("application/json")
    ) else {}
    body = body if isinstance(body, dict) else {}
    mode = str(body.get("mode") or "pty").strip().lower()
    opus = bool(body.get("opus", False))

    model = "opus" if opus else "sonnet"
    flags = f"{build_claude_flags(cfg, model_override=model)} /{_RECAP_COMMAND}"
    kind = "remote" if mode == "remote" else "pty"
    result = await _spawn_skill_session(
        cfg, request, life_os_dir,
        flags=flags, name=_RECAP_COMMAND, kind=kind, opus=opus, resume=False,
        audit_skill="_recap", body=body,
    )
    return {"launched": _RECAP_COMMAND, **result}


@router.post("/api/life-os/skills/{skill_id}/launch")
async def launch_skill(skill_id: str, request: Request) -> Dict[str, Any]:
    """Launch a claude session that auto-invokes ``/<skill>`` in life-os.

    Body: ``{"mode": "pty"|"remote", "opus": bool, "resume": bool}``. The
    cwd is fixed to ``life_os_dir``; the model is ``opus`` when the toggle
    is on, else ``sonnet``; the positional prompt is a bare ``/<skill>``
    (no free text). Mirrors the Coding tab's claude-code launch (PTY
    streamed to the phone vs. detached console window, + PC mirror window
    + audit).

    Resume (issue #151) reopens Claude's own native session picker instead
    of invoking the skill: it **drops the ``/<skill>`` prompt** so the user
    lands on the picker to pick up a prior conversation rather than starting
    the skill afresh. Resume is orthogonal to Detached (issue #157, matching
    the Coding tab): the requested ``mode`` still decides where the picker
    renders — a detached console window (``mode="remote"``) or a streamed PTY
    (``mode="pty"``). Resume no longer forces a PTY.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    life_os_dir = Path(cfg.life_os_dir)
    if not life_os_dir.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"life_os_dir does not exist: {cfg.life_os_dir}",
        )
    skill = _resolve_skill(cfg, skill_id)

    body = await request.json() if (
        request.headers.get("content-type", "").startswith("application/json")
    ) else {}
    body = body if isinstance(body, dict) else {}
    mode = str(body.get("mode") or "pty").strip().lower()
    opus = bool(body.get("opus", False))
    resume = bool(body.get("resume", False))

    # Model override is per-launch (opus toggle); the rest of the flags
    # (effort / permission / verbose / debug) come from the shared Coding
    # options. The bare /<skill> is appended as claude's positional prompt
    # — skill.command is a validated slug, so no shell-quoting is needed.
    # On Resume we drop the /<skill> prompt and swap in `claude --resume`
    # so the native picker shows instead of re-invoking the skill.
    model = "opus" if opus else "sonnet"
    if resume:
        flags = build_resume_flags(cfg, "claude", model_override=model)
    else:
        flags = (
            f"{build_claude_flags(cfg, model_override=model)} /{skill.command}"
        )
    name = skill.name

    # Detached and Resume are orthogonal (issue #157, matching the Coding
    # tab): the requested mode decides where the session renders — a detached
    # console (remote) or a streamed PTY — independent of resume.
    kind = "remote" if mode == "remote" else "pty"
    result = await _spawn_skill_session(
        cfg, request, life_os_dir,
        flags=flags, name=name, kind=kind, opus=opus, resume=resume,
        audit_skill=skill.id, body=body,
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
    audit.audit_event(
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
    audit.audit_event("lifeos_delete", path=rel, client=client_ip(request))
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
    body = await request.json() if (
        request.headers.get("content-type", "").startswith("application/json")
    ) else {}
    body = body if isinstance(body, dict) else {}
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
    audit.audit_event(
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
