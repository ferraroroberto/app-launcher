"""Removing the Claude Code transcript a Life OS conversation was captured from (#1410).

A Life OS capture (``<skill>/conversations/<date>-<slug>.md``) is a *copy*:
Claude Code keeps the conversation itself at
``~/.claude/projects/<project>/<session-id>.jsonl``, with an optional
``<session-id>/`` sidecar folder beside it, and that file is what Resume
reattaches to. Deleting a conversation completely means removing both.

The match is by **exact session id** and nothing else. This is the opposite of
the "newest ``*.jsonl`` in the folder" heuristic ``src/session_view/transcript_locate.py``
uses to *read* a live session: a wrong guess there shows the wrong card, a wrong
guess here destroys someone else's conversation. So the id must be a canonical
UUID (the same by-construction check the resume path applies), the file name is
built from it rather than searched for, and a project folder that is a symlink
or junction is never entered, so no path can leave ``~/.claude/projects``.

Blocking filesystem work: callers run it off the event loop.
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.life_os.links import is_link

logger = logging.getLogger(__name__)

# A canonical UUID, validated strictly: it becomes part of a file name, so a
# near-miss is refused rather than repaired.
SESSION_ID_RE = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")

# Verdicts for ``removed["transcript"]`` — one name per distinct condition so a
# "nothing there" is never read as "removed".
REMOVED = "removed"
NOT_FOUND = "not_found"
NO_SESSION_ID = "no_session_id"
NOT_CLAUDE = "not_claude"


def claude_projects_dir() -> Path:
    """``~/.claude/projects``, resolved per call so a test's temporary home applies."""
    return Path.home() / ".claude" / "projects"


def _project_folders(projects_dir: Path) -> List[Path]:
    """Real (non-link) directories directly under ``projects_dir``."""
    try:
        return [child for child in projects_dir.iterdir()
                if child.is_dir() and not is_link(child)]
    except OSError:
        return []


def _discard(target: Path, dest: Optional[Path]) -> None:
    """Remove ``target``, or move it into ``dest`` when a backup folder is given."""
    if dest is None:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return
    dest.mkdir(parents=True, exist_ok=True)
    landing = dest / target.name
    if landing.exists():
        raise FileExistsError(f"backup already holds {target.name}")
    shutil.move(str(target), str(landing))


def remove_claude_transcript(
    projects_dir: Path, agent: str, sid: str, *, dest: Optional[Path] = None,
) -> Dict[str, Any]:
    """Delete ``<project>/<sid>.jsonl`` and its ``<sid>/`` sidecar, by exact id.

    With ``dest`` the two entries are *moved* into that folder instead (the
    zombie cleanup's backup, #1738): same lookup, same link/escape jail, and a
    name already present in ``dest`` raises rather than being overwritten.

    Returns ``{"status", "files", "folders", "project"}``: ``status`` is one of
    :data:`REMOVED`, :data:`NOT_FOUND`, :data:`NO_SESSION_ID`,
    :data:`NOT_CLAUDE`; ``files`` / ``folders`` count what was actually removed;
    ``project`` names the project folder they came from (``""`` when nothing
    was found), which a restore needs to put them back.
    Raises ``OSError`` when something that exists could not be removed, so the
    caller can stop before it touches anything else.
    """
    result: Dict[str, Any] = {"status": NOT_FOUND, "files": 0, "folders": 0, "project": ""}
    if agent != "claude":
        result["status"] = NOT_CLAUDE
        return result
    if not SESSION_ID_RE.fullmatch(sid or ""):
        result["status"] = NO_SESSION_ID
        return result
    sid = sid.lower()
    root = projects_dir.resolve()
    for folder in _project_folders(projects_dir):
        transcript = folder / f"{sid}.jsonl"
        sidecar = folder / sid
        # Belt and braces on top of the by-construction name: the entry must
        # still resolve inside the projects folder.
        for target in (transcript, sidecar):
            if not (target.exists() or target.is_symlink()):
                continue
            if is_link(target) or root not in target.resolve().parents:
                logger.warning("⚠️ Life OS delete: not removing a linked or escaping entry")
                continue
            if target is transcript and target.is_file():
                _discard(target, dest)
                result["files"] += 1
                result["project"] = folder.name
            elif target is sidecar and target.is_dir():
                _discard(target, dest)
                result["folders"] += 1
                result["project"] = folder.name
    if result["files"] or result["folders"]:
        result["status"] = REMOVED
    return result

