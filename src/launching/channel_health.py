"""Whether a running Telegram channel session can still hear its bot.

A channel session is a Claude process that runs the Telegram plugin's server
(Bun) as a child. Claude can start a session without that server and say
nothing: a plugin it believes is not installed, or a server it recently saw
fail and now skips. The session then looks alive while the bot receives
nothing. So a running session counts as healthy only when its Claude process
has a Bun process beneath it.

The Claude process is found by its command line: every channel launch passes
its own settings file, ``<runtime data>/channel-settings/<profile id>.json``,
and no other process carries that path.

:func:`assess` folds what was seen into one state: ``ok``, ``degraded`` (with a
reason), ``starting`` (too young to judge) or ``unknown`` (the process table
could not be read, or the session's Claude process was not found). An unknown
is never folded into ``ok`` or ``degraded``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Collection, Dict, List, Mapping, Optional, Tuple

import psutil

from src.launching.channel_profiles import SETTINGS_DIR_NAME

logger = logging.getLogger(__name__)

OK = "ok"
DEGRADED = "degraded"
STARTING = "starting"
UNKNOWN = "unknown"

# The plugin's ``.mcp.json`` runs its server with ``bun``.
SERVER_NAMES = frozenset({"bun", "bun.exe"})
_CLAUDE_NAMES = frozenset({"claude", "claude.exe"})
# A fresh session starts its server within seconds; until this age a missing
# server is not yet a fault.
STARTUP_GRACE_SECONDS = 90.0

REASON_NO_SERVER = (
    "the Telegram plugin server is not running, so the bot receives nothing"
)
REASON_NO_SCAN = "the process table could not be read"
REASON_NO_PROCESS = "the session's Claude process was not found"
REASON_STARTING = "the session started moments ago"


@dataclass(frozen=True)
class ProcRow:
    pid: int
    ppid: int
    name: str
    cmdline: Tuple[str, ...]


def snapshot() -> Optional[List[ProcRow]]:
    """The process table, or ``None`` when it cannot be read at all. A
    process whose command line is denied keeps its row with no arguments."""
    rows: List[ProcRow] = []
    try:
        for proc in psutil.process_iter(["pid", "ppid", "name", "cmdline"]):
            info = proc.info
            rows.append(ProcRow(
                pid=int(info.get("pid") or 0),
                ppid=int(info.get("ppid") or 0),
                name=str(info.get("name") or "").lower(),
                cmdline=tuple(info.get("cmdline") or ()),
            ))
    except (psutil.Error, OSError) as exc:
        logger.warning(f"⚠️ channel health: process table unreadable ({exc})")
        return None
    return rows


def _profile_of(row: ProcRow, profile_ids: Collection[str]) -> str:
    """The profile id whose settings file is on ``row``'s command line."""
    for arg in row.cmdline:
        path = str(arg).replace("\\", "/")
        for pid in profile_ids:
            if path.endswith(f"/{SETTINGS_DIR_NAME}/{pid}.json"):
                return pid
    return ""


def server_states(
    profile_ids: Collection[str], rows: List[ProcRow]
) -> Dict[str, Optional[bool]]:
    """Per profile id: ``True`` when its Claude process has a Bun process
    beneath it, ``False`` when the process is there without one, ``None``
    when no Claude process for it was found."""
    children: Dict[int, List[ProcRow]] = {}
    for row in rows:
        children.setdefault(row.ppid, []).append(row)
    states: Dict[str, Optional[bool]] = {pid: None for pid in profile_ids}
    for row in rows:
        if row.name not in _CLAUDE_NAMES:
            continue
        pid = _profile_of(row, profile_ids)
        if not pid:
            continue
        states[pid] = bool(states[pid]) or _has_server(row.pid, children)
    return states


def _has_server(root: int, children: Mapping[int, List[ProcRow]]) -> bool:
    stack, seen = [root], {root}
    while stack:
        for child in children.get(stack.pop(), ()):
            if child.name in SERVER_NAMES:
                return True
            if child.pid not in seen:
                seen.add(child.pid)
                stack.append(child.pid)
    return False


def assess(
    sess: Optional[Mapping[str, Any]],
    server: Optional[bool],
    scanned: bool,
    now: float,
) -> Tuple[Optional[str], str]:
    """``(state, reason)`` for one profile; ``(None, "")`` when it is not
    running (or whether it runs is not known), which is not a health fault."""
    if not sess:
        return None, ""
    if server is True:
        return OK, ""
    started = sess.get("started_at")
    if (
        isinstance(started, (int, float)) and not isinstance(started, bool)
        and now - float(started) < STARTUP_GRACE_SECONDS
    ):
        return STARTING, REASON_STARTING
    if not scanned:
        return UNKNOWN, REASON_NO_SCAN
    if server is None:
        return UNKNOWN, REASON_NO_PROCESS
    return DEGRADED, REASON_NO_SERVER
