"""Launcher-owned storage for a Board issue-start dispatch brief (issue #1114).

A chief-dispatched lane trusts exactly one thing as coming from the operator:
its launch command (fleet-config#944). ``POST /api/board/issues/start`` can
therefore carry an optional free-text **brief** — the dispatcher's scope,
queue and constraints — but that text must never reach the session-host's
unquoted ``cmd /c`` line. So the brief is written here, to a file whose name
is a launcher-generated uuid, and only that *path* is appended to the prompt
as ``--brief <path>``. No client-derived character reaches the command line;
:func:`write_brief` asserts it rather than assuming it.

Store: ``<audit dir>/briefs/<uuid hex>.md`` — the same directory
``LAUNCHER_AUDIT_DIR`` relocates for :mod:`src.audit` (issue #913), so a test
or e2e-gate run never writes into the checkout's live ``webapp/``. Gitignored.

Bounded: every write first prunes briefs older than :data:`BRIEF_TTL` and
then keeps at most :data:`MAX_BRIEF_FILES` of the newest, so the directory
cannot grow without limit however many dispatches run. The TTL is far longer
than any lane needs to read its brief (step 1, within seconds of launch).
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Optional

from src.audit import AUDIT_DIR_ENV, PROJECT_ROOT

logger = logging.getLogger(__name__)

MAX_BRIEF_CHARS = 20_000
BRIEF_TTL = 7 * 24 * 3600
MAX_BRIEF_FILES = 200

# Every character the path may carry on the command line. A launcher-built
# path under a sane install root never trips this; one that does (a space or
# a cmd metacharacter in the install directory) is refused rather than quoted.
_SAFE_PATH = re.compile(r"^[A-Za-z0-9:/._-]+$")


class BriefError(ValueError):
    """A client-supplied brief that must be refused with a 400."""


def _resolve_briefs_dir() -> Path:
    # Mirrors src.audit's resolution of the same variable, so briefs follow
    # the audit files wherever LAUNCHER_AUDIT_DIR sends them (#913).
    override = os.environ.get(AUDIT_DIR_ENV, "").strip()
    return (Path(override) if override else PROJECT_ROOT / "webapp") / "briefs"


BRIEFS_DIR = _resolve_briefs_dir()


def validate_brief(raw: object) -> str:
    """Return the brief text, or raise :class:`BriefError` naming why not."""
    if not isinstance(raw, str):
        raise BriefError("brief must be a string")
    if not raw.strip():
        raise BriefError("brief is empty")
    if len(raw) > MAX_BRIEF_CHARS:
        raise BriefError(
            f"brief too large: {len(raw)} chars (max {MAX_BRIEF_CHARS})"
        )
    return raw


def prune_briefs(now: Optional[float] = None) -> int:
    """Drop expired briefs, then all but the newest ``MAX_BRIEF_FILES``."""
    try:
        entries = [
            (p.stat().st_mtime, p) for p in BRIEFS_DIR.glob("*.md") if p.is_file()
        ]
    except OSError as exc:
        logger.warning("⚠️ Could not list dispatch briefs for pruning: %s", exc)
        return 0
    cutoff = (now if now is not None else time.time()) - BRIEF_TTL
    entries.sort(reverse=True)
    doomed = [
        p for i, (mtime, p) in enumerate(entries)
        if mtime < cutoff or i >= MAX_BRIEF_FILES
    ]
    removed = 0
    for path in doomed:
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            logger.warning("⚠️ Could not prune dispatch brief %s: %s", path.name, exc)
    if removed:
        logger.info("ℹ️ Pruned %d dispatch brief file(s)", removed)
    return removed


def write_brief(text: str) -> Path:
    """Persist a validated brief under a fresh uuid name and return its path."""
    BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    prune_briefs()
    path = BRIEFS_DIR / f"{uuid.uuid4().hex}.md"
    if not _SAFE_PATH.match(path.as_posix()):
        raise RuntimeError(
            f"brief directory is not command-line safe: {BRIEFS_DIR.as_posix()!r}"
        )
    path.write_text(text, encoding="utf-8")
    return path


def discard_brief(path: Path) -> None:
    """Best-effort removal of a brief whose launch never happened."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("⚠️ Could not discard dispatch brief %s: %s", path.name, exc)
