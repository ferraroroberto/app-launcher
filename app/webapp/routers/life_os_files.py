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

import asyncio
import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from src import audit
from src._json_io import atomic_write_json
from src.scanner import skills_dir_for
from src.subprocess_flags import NO_WINDOW
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
# Mirrors life_os_conversations._CONVERSATIONS_INDEX — the digested index a
# conversation log's own directory carries alongside it (#906).
_CONVERSATIONS_INDEX = "index.json"
# fleet-config's conversation_index.py: INDEX_NAME — the human-readable
# source of truth index.json is itself regenerated from (#971). Renaming a
# capture without also updating this file's matching ``file="..."`` entry
# means the external pipeline's next run can silently overwrite or orphan
# the index.json patch below.
_CONVERSATIONS_INDEX_MD = "index.md"
# fleet-config's
# cross-skill search CLI, resolved per call so a Settings change to
# claude_config_dir takes effect without a restart (#971).
_SEARCH_SCRIPT_REL = ("hooks", "conversation_search.py")
_RESYNC_TIMEOUT_S = 30


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


def _patch_conversation_index(
    index_path: Path, mutate: Callable[[List[Any]], Optional[List[Any]]]
) -> None:
    """Best-effort read-mutate-write of a skill's ``conversations/index.json``.

    The index is written by an external capture/index pipeline (life-os#68) —
    this repo doesn't own its format, so any read/parse/write failure is
    logged and swallowed rather than failing the delete/rename that triggered
    it (#906). ``mutate`` returns the new row list, or ``None`` to skip the
    write (nothing matched).
    """
    try:
        raw = index_path.read_text(encoding="utf-8")
    except OSError:
        return
    try:
        rows = json.loads(raw)
    except ValueError:
        logger.warning("⚠️ unreadable conversation index, leaving as-is: %s", index_path)
        return
    if not isinstance(rows, list):
        return
    updated = mutate(rows)
    if updated is None:
        return
    try:
        atomic_write_json(index_path, updated)
    except OSError as exc:
        logger.warning("⚠️ could not update conversation index: %s", exc)


def _prune_conversation_index(resolved: Path) -> None:
    """Drop ``resolved``'s row from its skill's digested index, if present (#906).

    Keeps the Conversations view from showing a just-deleted log until the
    external indexer next runs — see ``_patch_conversation_index``.
    """
    def _drop(rows: List[Any]) -> Optional[List[Any]]:
        kept = [r for r in rows if not (isinstance(r, dict) and r.get("file") == resolved.name)]
        return kept if len(kept) != len(rows) else None
    _patch_conversation_index(resolved.parent / _CONVERSATIONS_INDEX, _drop)


def _rename_conversation_index(resolved: Path, new_name: str) -> None:
    """Point ``resolved``'s row at its new filename, if present (#906)."""
    def _rename_row(rows: List[Any]) -> Optional[List[Any]]:
        changed = False
        for row in rows:
            if isinstance(row, dict) and row.get("file") == resolved.name:
                row["file"] = new_name
                changed = True
        return rows if changed else None
    _patch_conversation_index(resolved.parent / _CONVERSATIONS_INDEX, _rename_row)


def _rename_index_md(resolved: Path, new_name: str) -> None:
    """Point ``resolved``'s ``<!-- idx file="..." -->`` entry at its new name (#971).

    Best-effort exact substring swap of the ``file="<old>"`` attribute —
    conservative on purpose: this repo doesn't own ``index.md``'s format, so
    it touches only the one attribute value it knows how to identify safely,
    never the surrounding heading/body prose. A miss (attribute not found,
    file unreadable) is silently skipped rather than failing the rename.
    """
    index_md = resolved.parent / _CONVERSATIONS_INDEX_MD
    try:
        text = index_md.read_text(encoding="utf-8")
    except OSError:
        return
    marker = f'file="{resolved.name}"'
    if marker not in text:
        return
    try:
        index_md.write_text(text.replace(marker, f'file="{new_name}"', 1), encoding="utf-8")
    except OSError as exc:
        logger.warning("⚠️ could not update conversations index.md: %s", exc)


# The comment fleet-config's conversation_index.py::render_index() starts a
# decay-zone tail with — the one other block boundary an entry-removal scan
# must respect (never eat into hand-preserved period summaries below it).
_INDEX_MD_DECAY_MARKER_PREFIX = "<!-- decay-zone:"


def _prune_index_md(resolved: Path) -> None:
    """Drop ``resolved``'s whole ``<!-- idx file="..." -->`` entry from index.md (#971).

    Deterministic line-based removal matching the exact block shape
    ``conversation_index.py``'s own ``render_index()`` writes — one ``idx``
    comment line, one ``###`` heading line, the body lines, one blank
    separator — found by the same exact-attribute marker ``_rename_index_md``
    uses, and cut at the next entry / decay-zone marker / EOF, whichever
    comes first. Never imports or shells into that pipeline: this is pure
    text surgery, so pruning a stale entry can never trigger an LLM re-digest
    of some unrelated capture in the same directory. A shape mismatch (no
    match, custom edits) is a silent no-op, same stance as the rest of this
    best-effort file.
    """
    index_md = resolved.parent / _CONVERSATIONS_INDEX_MD
    try:
        lines = index_md.read_text(encoding="utf-8").split("\n")
    except OSError:
        return
    marker = f'file="{resolved.name}"'
    start = next(
        (i for i, ln in enumerate(lines) if ln.startswith("<!-- idx ") and marker in ln),
        None,
    )
    if start is None:
        return
    end = start + 1
    while (
        end < len(lines)
        and not lines[end].startswith("<!-- idx ")
        and not lines[end].startswith(_INDEX_MD_DECAY_MARKER_PREFIX)
    ):
        end += 1
    try:
        index_md.write_text("\n".join(lines[:start] + lines[end:]), encoding="utf-8")
    except OSError as exc:
        logger.warning("⚠️ could not update conversations index.md: %s", exc)


def search_cli(cfg: WebappConfig) -> Optional[List[str]]:
    """``[python, script]`` for fleet-config's search CLI, or ``None`` (#971).

    Resolved per request from ``claude_config_dir`` (the fleet-config
    checkout the Board already shells into) so pointing Settings at a
    different checkout takes effect without a restart. ``None`` when either
    half is missing — a machine without fleet-config still gets a working
    Life OS tab, minus search.

    The single copy (#1003). ``life_os_conversations`` used to carry a
    byte-identical body and its own ``_SEARCH_SCRIPT_REL``; it already
    imports :func:`resolve_within` from here, so the import edge ran in the
    right direction and this module stays the leaf its docstring describes.
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


def _resync_search_index(cfg: WebappConfig) -> None:
    """Best-effort rebuild fleet-config's cross-skill search db (#971).

    Without this, a rename/delete this endpoint just made stays invisible to
    ``conversations/.search.db`` (built by fleet-config's
    ``hooks/conversation_search.py``) until an unrelated external
    capture/index run happens to resync it — so a Search hit on the changed
    conversation stays resolvable under its stale pre-change identity and
    404s through ``read_captures`` when opened. ``--rebuild`` is the
    pipeline's own documented self-heal entry point (a pure derivative
    rebuild from what's on disk); this repo doesn't own the db's format, so
    every failure mode here (no checkout, locked db, slow machine) is logged
    and swallowed rather than failing the rename/delete that triggered it.
    """
    cli = search_cli(cfg)
    if cli is None:
        return
    try:
        proc = subprocess.run(
            [*cli, "--cwd", str(cfg.life_os_dir), "--rebuild"],
            capture_output=True, timeout=_RESYNC_TIMEOUT_S, creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("⚠️ Life OS search-index resync unavailable: %s", exc)
        return
    if proc.returncode != 0:
        logger.warning(
            "⚠️ Life OS search-index resync exited %s", proc.returncode
        )


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
    _prune_conversation_index(resolved)
    _prune_index_md(resolved)
    await asyncio.to_thread(_resync_search_index, cfg)
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
    _rename_conversation_index(resolved, target.name)
    _rename_index_md(resolved, target.name)
    await asyncio.to_thread(_resync_search_index, cfg)
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
