"""Life OS private-content file browser — read, delete and rename, path-jailed.

    GET    /api/life-os/file?path=…            → file content (Tailscale + passkey)
    DELETE /api/life-os/file?path=…            → delete one conversation log
    POST   /api/life-os/file/rename            → rename one conversation log

Split off ``app/webapp/routers/life_os.py`` (issue #884, a `/codebase-audit`
maintainability finding) and mounted on ``life_os.router`` via
``include_router``, so ``app/webapp/server.py`` still registers one
``life_os.router``. A leaf: it imports no other Life OS module. ``life_os.py``
lists a skill's tree through :func:`_walk_files`, and the conversations router
jails its capture paths through :func:`resolve_within`.

The content endpoints surface private, gitignored knowledge
(``context/`` ``memory/`` ``examples/`` ``conversations/`` + the shared
``identity/``). They are gated like the live terminal — refused over the
Cloudflare tunnel, Tailscale-only, passkey-required (see
``app/webapp/middleware.py``) — and the file-content endpoint is
**path-jailed** to ``life_os_dir`` (the jail is the whole security story
for an endpoint that reads arbitrary files under a root).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from src import audit, life_os_index
from src.scanner import skills_dir_for
from src.webapp_config import WebappConfig

from app.webapp.routers._helpers import audit_off_loop, client_ip, maybe_json

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
    await life_os_index.reconcile_delete(resolved, cfg)
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
    await life_os_index.reconcile_rename(resolved, target.name, cfg)
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
    wastes horizontal space (#118). Sorted by category then path, except
    ``conversations`` — date-prefixed filenames, shown newest-first like
    the digested conversation index endpoint below (#863).
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
    # The sort above groups conversations into one contiguous run (category
    # is the primary key) — reverse just that run so the newest log shows
    # first, leaving every other category's A-Z order untouched.
    convos = [f for f in out if f["category"] == "conversations"]
    if convos:
        start = out.index(convos[0])
        out[start:start + len(convos)] = reversed(convos)
    return out
