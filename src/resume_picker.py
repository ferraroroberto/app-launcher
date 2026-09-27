"""Claude Code's ``/resume`` picker, answered from a Chat card (#1300).

Resuming from the phone meant driving Claude Code's own session picker in
the terminal: a long, scrolling TUI list. The Chat pane shows that list as a
searchable card instead, and a pick resumes the session deterministically.
Probed on Claude Code 2.1.283 (the probe is recorded on #1300):

* ``/resume <session-id>`` typed into a running interactive session resumes
  exactly that conversation, no picker in between. So a pick leaves the
  picker (Escape, which prints "Resume cancelled") and types that command,
  rather than steering the picker's scrolling list.
* Only interactive conversations are resumable that way. A print-mode
  (``claude -p``) transcript carries ``"entrypoint": "sdk-cli"`` and
  ``/resume <its id>`` answers "No conversations found to resume.", so the
  card lists everything *but* those, as the picker itself does.

What the picker looks like (120 and 52 columns)::

    ──────────────────────────────────────────
      Resume session
      ╭────────────────────────────────────╮
      │ ⌕ Search…                          │
      ╰────────────────────────────────────╯
        my-project
      ❯ Fix the login redirect
        10 seconds ago · main · 280.4KB
        Add a dark theme
        1 minute ago · main · 120.1KB
        Ctrl+A to show all projects · Ctrl+B to only show current
        branch · Space to preview · Ctrl+R to rename · Type to search ·
        Esc to cancel

The card's list comes from the transcripts on disk, not from this screen
(the screen shows a scrolled window of it). Only a title-or-first-prompt
line and a time ever leave this module: never the transcript text.

Pure apart from the transcript reads.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.ask_user_question import Keystroke
from src.board_exchange import _claude_declared_titles, _claude_project_folders

HEADING = "Resume session"
SEARCH_GLYPH = "⌕"  # ⌕
FOOTER_END = "Esc to cancel"
NOT_FOUND = "No conversations found to resume."
CANCELLED = "Resume cancelled"

# A print-mode transcript: not resumable with /resume <id> (probed).
PRINT_MODE_ENTRYPOINT = "sdk-cli"

TITLE_CAP = 120
LIST_CAP = 200
# Enough head to reach the first user line past the opening metadata rows
# (mode, permission-mode, attachments of a long CLAUDE.md run to ~200 KB).
HEAD_BYTES = 512 * 1024
ESCAPE = "\x1b"

_SESSION_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_COMMAND_NAME_RE = re.compile(r"<command-name>\s*(/[^<\s]+)\s*</command-name>")


def parse_resume_picker(lines: List[str]) -> bool:
    """Whether this screen shows the ``/resume`` picker now.

    Strict on purpose: the "Resume session" heading, the search box right
    under it, and the key-hint footer ending "Esc to cancel" as the last
    thing on the screen. Anything below the footer (a prompt, a reply, a
    status line) means the picker is not what the terminal shows now.
    """
    rows = [line.rstrip() for line in lines]
    heading = next(
        (i for i in range(len(rows) - 1, -1, -1) if rows[i].strip() == HEADING), None
    )
    if heading is None:
        return False
    search = next((i for i in range(heading + 1, min(heading + 4, len(rows)))
                   if SEARCH_GLYPH in rows[i]), None)
    if search is None:
        return False
    tail = [r.strip() for r in rows[search + 1:] if r.strip()]
    if not tail:
        return False
    # The footer wraps over several rows at a narrow width; its end is the
    # last non-blank row on the screen.
    return " ".join(tail[-4:]).endswith(FOOTER_END)


def _first_prompt(path: Path) -> Optional[Dict[str, str]]:
    """``{"entrypoint", "prompt"}`` from a transcript's head, or ``None``
    when it holds no user turn (the picker lists none of those either).

    The prompt is the first user message's text, or the slash command it
    ran (``/exit``), which is what the picker shows for such a session.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(HEAD_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None
    entrypoint = ""
    for line in head.splitlines():
        if '"type":"user"' not in line.replace(" ", ""):
            if not entrypoint and '"entrypoint"' in line:
                try:
                    entrypoint = str(json.loads(line).get("entrypoint") or "")
                except (TypeError, ValueError):
                    pass
            continue
        try:
            obj = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(obj, dict) or obj.get("isSidechain"):
            continue
        entrypoint = entrypoint or str(obj.get("entrypoint") or "")
        content = (obj.get("message") or {}).get("content")
        if isinstance(content, list):
            content = " ".join(
                str(part.get("text") or "") for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        text = str(content or "").strip()
        if not text:
            continue
        command = _COMMAND_NAME_RE.search(text)
        if command:
            return {"entrypoint": entrypoint, "prompt": command.group(1)}
        if text.startswith("<"):
            continue  # a caveat or other wrapper the picker never shows
        return {"entrypoint": entrypoint, "prompt": text}
    return None


def _one_line(text: str) -> str:
    return " ".join(str(text or "").split())[:TITLE_CAP]


def list_sessions(project_dir: str) -> Optional[List[Dict[str, Any]]]:
    """The conversations ``/resume`` offers for ``project_dir``, newest first:
    ``[{"id", "title", "updated_at"}]``. ``None`` when the projects folder
    can't be read (distinct from an empty list: nothing to resume).

    Top-level ``*.jsonl`` only (a subagent's transcript lives in a subfolder),
    minus print-mode ones and those with no user turn. The title is the
    newest custom title, else the newest AI title, else the first prompt.
    """
    folders = _claude_project_folders(project_dir) if project_dir else None
    if folders is None:
        return None
    found: List[Dict[str, Any]] = []
    for folder in folders:
        try:
            paths = [p for p in folder.glob("*.jsonl") if p.is_file()]
        except OSError:
            continue
        for path in paths:
            if not _SESSION_ID_RE.match(path.stem):
                continue
            head = _first_prompt(path)
            if head is None or head["entrypoint"] == PRINT_MODE_ENTRYPOINT:
                continue
            custom, ai = _claude_declared_titles(path)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            found.append({
                "id": path.stem,
                "title": _one_line(custom or ai or head["prompt"]),
                "updated_at": datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
                "_mtime": mtime,
            })
    found.sort(key=lambda s: s["_mtime"], reverse=True)
    return [{k: v for k, v in s.items() if k != "_mtime"} for s in found[:LIST_CAP]]


def resume_keys(sessions: Optional[List[Dict[str, Any]]], session_id: Any) -> List[Keystroke]:
    """The command for one pick, checked against ``sessions`` (a fresh
    listing at send time, never the client's copy).

    Raises :class:`ValueError` for a malformed id and
    :class:`ResumeRefused` when the id is not one ``/resume`` offers here.
    """
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        raise ValueError("session_id must be a Claude Code session id")
    if sessions is None:
        raise ResumeRefused("Chat can't read this project's sessions, so nothing was sent")
    if not any(s["id"] == session_id for s in sessions):
        raise ResumeRefused("That session is not in this project's list any more: nothing was sent")
    return [(f"/resume {session_id}", True)]


def resume_outcome(lines: List[str], session_id: str) -> Optional[str]:
    """What the screen says about a ``/resume <id>`` just typed:
    ``"not_found"`` or ``"cancelled"`` when Claude Code answered it with one
    of those lines, ``None`` when it says neither (resumed, or not yet)."""
    echo = f"/resume {session_id}"
    rows = [line.strip() for line in lines]
    at = next((i for i in range(len(rows) - 1, -1, -1) if rows[i].endswith(echo)), None)
    if at is None:
        return None
    for row in rows[at + 1:at + 4]:
        if NOT_FOUND in row:
            return "not_found"
        if CANCELLED in row:
            return "cancelled"
    return None


class ResumeRefused(Exception):
    """The pick is not something ``/resume`` offers now: a 409, never a retry."""
