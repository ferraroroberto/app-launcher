"""Reconciling the artefacts a Life OS conversation mutation leaves behind.

Split out of ``app/webapp/routers/life_os_files.py`` (a ``/codebase-audit``
maintainability finding, #1006), where 159 of the router's 501 lines were
best-effort maintenance of two artefacts the module explicitly disclaims
owning, dwarfing the three routes they support.

**This repo does not own any of these formats.** ``index.json`` and
``index.md`` are written by fleet-config's external capture/index pipeline,
and ``.search.db`` is built by its ``conversation_search.py``. So every
operation here is best-effort: a read, parse, write or subprocess failure is
logged and swallowed rather than failing the delete or rename that triggered
it (#906/#971). That single contract used to be restated in seven separate
docstrings; it belongs to the module, which is the clearest argument for the
module existing.

The router calls one entry point per mutation - :func:`reconcile_delete`,
:func:`reconcile_rename` - and no longer has to know that there are three
artefacts or in which order they must be touched.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Callable, List, Optional

from src._json_io import atomic_write_json
from src.subprocess_flags import NO_WINDOW
from src.webapp_config import WebappConfig

logger = logging.getLogger(__name__)

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


async def reconcile_delete(resolved: Path, cfg: WebappConfig) -> None:
    """Bring the external artefacts in line with a deleted conversation.

    The on-loop/off-thread split is deliberate and unchanged from when the
    router did this inline: the two index patches are small local file
    rewrites, while the search-db rebuild shells out and is given a thread.
    """
    _prune_conversation_index(resolved)
    _prune_index_md(resolved)
    await asyncio.to_thread(_resync_search_index, cfg)


async def reconcile_rename(resolved: Path, new_name: str, cfg: WebappConfig) -> None:
    """Same, for a renamed conversation. ``resolved`` is the pre-rename path."""
    _rename_conversation_index(resolved, new_name)
    _rename_index_md(resolved, new_name)
    await asyncio.to_thread(_resync_search_index, cfg)
