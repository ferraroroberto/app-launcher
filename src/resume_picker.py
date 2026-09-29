"""Claude Code's ``/resume`` picker, answered from a Chat card (#1300).

Resuming from the phone meant driving Claude Code's own session picker in
the terminal: a long, scrolling TUI list. The Chat pane shows that list as a
searchable card instead, and a pick resumes the session deterministically.
Probed on Claude Code 2.1.283 (the probe is recorded on #1300):

* ``/resume <session-id>`` typed into a running interactive session resumes
  exactly that conversation, no picker in between. A pick made with no
  picker on screen (``/resume`` typed in Chat) types that command.
* A pick made *while the picker is up* never leaves it with Escape: on the
  picker a ``claude --resume`` launch opens with, Escape exits Claude Code
  altogether (probed at 51 columns: the process was gone a few seconds
  later). It steers the picker instead. Down moves the highlight one row and
  wraps from the last row to the first, so one Down per row reaches every
  row from anywhere. The route re-reads the screen after each Down and
  presses Enter only once the highlighted row can be nothing but the chosen
  conversation (:func:`cursor_on`). A digit is never typed: it picks that
  row outright.
* The picker orders rows by last message, not by file time, so a row's
  position says nothing about which conversation it is: only its title does.
* Only interactive conversations are resumable that way. A print-mode
  (``claude -p``) transcript carries ``"entrypoint": "sdk-cli"`` and
  ``/resume <its id>`` answers "No conversations found to resume.", so the
  card lists everything *but* those, as the picker itself does.

What the picker looks like (the ``--resume`` launch at 51 columns; a short
list drops the ``(1 of 50)`` count, which follows the highlight)::

    ───────────────────────────────────────────────────
      Resume session (1 of 50)
      ╭─────────────────────────────────────────────╮
      │ ⌕ Search…                                   │
      ╰─────────────────────────────────────────────╯
        my-project · my-project
      ❯ Fix the login redirect
        14 seconds ago · main · 554.6KB
        Add a dark theme
        25 minutes ago · main · 5.8MB ·
        example/my-project#12
      ↓ /remote-control is active · Continue here,…
        Ctrl+A to show all projects · Ctrl+B to only
        show current branch · Ctrl+W to show all
        worktrees · Space to preview · Ctrl+R to
        rename · Type to search · Esc to cancel

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
from typing import Any, Dict, List, Optional, Tuple

from src.ask_user_question import Keystroke
from src.transcript_locate import _claude_declared_titles, _claude_project_folders

# "Resume session", or "Resume session (3 of 50)" on a list long enough to
# scroll.
_HEADING_RE = re.compile(r"^Resume session(?: \((\d+) of (\d+)\))?$")
SEARCH_GLYPH = "⌕"  # ⌕
# The footer's last words: on the list, and with the search box focused (Up
# from the first row, or typing a search), where Escape clears the search.
FOOTER_ENDS = ("Esc to cancel", "Esc to clear")
NOT_FOUND = "No conversations found to resume."
CANCELLED = "Resume cancelled"

# A print-mode transcript: not resumable with /resume <id> (probed).
PRINT_MODE_ENTRYPOINT = "sdk-cli"

TITLE_CAP = 120
LIST_CAP = 200
# Enough head to reach the first user line past the opening metadata rows
# (mode, permission-mode, attachments of a long CLAUDE.md run to ~200 KB).
HEAD_BYTES = 512 * 1024
# The picker's highlighted row, and the keys that steer it (raw keystrokes).
CURSOR = "❯"  # ❯
DOWN = "\x1b[B"
ENTER = "\r"
ELLIPSIS = "…"  # …

_SESSION_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_COMMAND_NAME_RE = re.compile(r"<command-name>\s*(/[^<\s]+)\s*</command-name>")


def _picker_rows(lines: List[str]) -> Optional[Tuple[List[str], int, Optional[int]]]:
    """``(rows, search_row, total)`` when this screen shows the ``/resume``
    picker now, else ``None``. ``total`` is the list length the heading
    counts, when it shows one.

    Strict on purpose: the "Resume session" heading, the search box right
    under it, and the key-hint footer ending "Esc to cancel" (or "Esc to
    clear" while the search box has focus) as the last thing on the
    screen. Anything below the footer (a prompt, a reply, a
    status line) means the picker is not what the terminal shows now.
    """
    rows = [line.rstrip() for line in lines]
    match = None
    heading = None
    for i in range(len(rows) - 1, -1, -1):
        match = _HEADING_RE.match(rows[i].strip())
        if match:
            heading = i
            break
    if heading is None:
        return None
    search = next((i for i in range(heading + 1, min(heading + 4, len(rows)))
                   if SEARCH_GLYPH in rows[i]), None)
    if search is None:
        return None
    tail = [r.strip() for r in rows[search + 1:] if r.strip()]
    if not tail:
        return None
    # The footer wraps over several rows at a narrow width; its end is the
    # last non-blank row on the screen.
    if not " ".join(tail[-4:]).endswith(FOOTER_ENDS):
        return None
    total = int(match.group(2)) if match.group(2) else None
    return rows, search, total


def parse_resume_picker(lines: List[str]) -> bool:
    """Whether this screen shows the ``/resume`` picker now."""
    return _picker_rows(lines) is not None


def picker_total(lines: List[str]) -> Optional[int]:
    """How many rows the picker's heading says it lists, or ``None`` (no
    picker, or a list short enough to show no count)."""
    found = _picker_rows(lines)
    return found[2] if found else None


def highlighted_title(lines: List[str]) -> Optional[str]:
    """The title on the picker's highlighted row, as the screen shows it
    (possibly cut short with an ellipsis). ``None`` when no picker is up or
    no row is highlighted (Up from the first row moves into the search box)."""
    found = _picker_rows(lines)
    if not found:
        return None
    rows, search, _total = found
    for row in rows[search + 1:]:
        text = row.strip()
        if text.startswith(CURSOR + " "):
            return text[len(CURSOR) + 1:].strip() or None
    return None


def _fold(text: str) -> str:
    return " ".join(str(text or "").split()).casefold()


def title_shows(shown: str, title: str) -> bool:
    """Whether a picker row showing ``shown`` could be the conversation
    titled ``title``. Either side may be cut short: the screen with an
    ellipsis at its width, the list at :data:`TITLE_CAP`."""
    a, b = _fold(shown), _fold(title)
    cut_a = a.endswith(ELLIPSIS)
    if cut_a:
        a = a[:-1].rstrip()
    cut_b = len(b) >= TITLE_CAP
    if not a or not b:
        return False
    if cut_a and cut_b:
        n = min(len(a), len(b))
        return a[:n] == b[:n]
    if cut_a:
        return b.startswith(a)
    if cut_b:
        return a.startswith(b)
    return a == b


def cursor_on(lines: List[str], sessions: List[Dict[str, Any]], session_id: str) -> str:
    """Where the picker's highlight is, measured against a pick:

    * ``"target"``: on a row that can only be ``session_id`` (Enter is safe);
    * ``"ambiguous"``: on a row that could be it, but could as well be
      another listed conversation;
    * ``"other"``: on some other row, or on none;
    * ``"closed"``: the picker is not on the screen.
    """
    if not parse_resume_picker(lines):
        return "closed"
    shown = highlighted_title(lines)
    if shown is None:
        return "other"
    hits = [s["id"] for s in sessions if title_shows(shown, s["title"])]
    if session_id not in hits:
        return "other"
    return "target" if len(hits) == 1 else "ambiguous"


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


def pick_session(sessions: Optional[List[Dict[str, Any]]], session_id: Any) -> Dict[str, Any]:
    """The listed conversation one pick names, checked against ``sessions``
    (a fresh listing at send time, never the client's copy).

    Raises :class:`ValueError` for a malformed id and
    :class:`ResumeRefused` when the id is not one ``/resume`` offers here.
    """
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        raise ValueError("session_id must be a Claude Code session id")
    if sessions is None:
        raise ResumeRefused("Chat can't read this project's sessions, so nothing was sent")
    found = next((s for s in sessions if s["id"] == session_id), None)
    if found is None:
        raise ResumeRefused("That session is not in this project's list any more: nothing was sent")
    return found


def resume_keys(sessions: Optional[List[Dict[str, Any]]], session_id: Any) -> List[Keystroke]:
    """The command for a pick made with no picker on screen
    (:func:`pick_session` checks it)."""
    pick_session(sessions, session_id)
    return [(f"/resume {session_id}", True)]


def steer_check(sessions: Optional[List[Dict[str, Any]]], session_id: Any) -> Dict[str, Any]:
    """:func:`pick_session` for a pick made on the picker, which is found by
    its title: refused up front when another listed conversation carries the
    same title, since no screen read could then tell the two apart."""
    found = pick_session(sessions, session_id)
    want = _fold(found["title"])
    if any(s["id"] != session_id and _fold(s["title"]) == want for s in sessions or []):
        raise ResumeRefused(
            "Another session here has the same title, so Chat can't tell them apart "
            "on the picker: pick it on the terminal. Nothing was sent"
        )
    return found


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
