"""Which Copilot session folder a resumed launch has on screen.

Every interactive launch creates a folder of its own. A conversation picked
afterwards (the ``--resume`` picker, ``/resume``) stays in its original
folder, which gets no line until the next prompt, so the launch's folder is
left without a single turn. ``--resume=<id>`` creates no folder at all: it
reopens the old one. What ties a launch to a reopened folder is
``inuse.<pid>.lock``: a Copilot process keeps one in every session folder it
has opened, stamped when it opened it, and drops them when it exits (probed on
CLI 1.0.116).
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import psutil

_LOCK_RE = re.compile(r"^inuse\.(\d+)\.lock$")
# Cheap byte filters; the parsed record's own ``type`` decides.
_USER_TURN_MARK = b'"user.message"'
_RESUME_MARK = b'"session.resume"'
# A log bigger than this is no empty launch folder, whatever it holds.
_EMPTY_LOG_MAX_BYTES = 512 * 1024
_RESUME_CHUNK_BYTES = 1024 * 1024
# How long a followed folder is kept: opening a conversation moves no mtime a
# ``stat`` of the listing would see, and the live tick asks every few seconds.
SHOWN_TTL_S = 10.0
_CACHE_MAX = 256
# Logs seen to hold a turn: a log only grows, so that never changes.
_HAS_TURN: Set[Path] = set()
# ``(state dir, launch folder) -> (scan instant, folder found)``.
_SHOWN_CACHE: Dict[Tuple[Path, Path], Tuple[float, Optional[Path]]] = {}
# Per event log: the byte offset scanned so far and the resume instants seen.
_RESUME_CACHE: Dict[Path, Tuple[int, List[float]]] = {}


def holds_no_turn(log: Path) -> bool:
    """True when ``log`` is readable and nobody has said anything in it."""
    if log in _HAS_TURN:
        return False
    try:
        with log.open("rb") as fh:
            head = fh.read(_EMPTY_LOG_MAX_BYTES + 1)
    except OSError:
        return False
    if len(head) <= _EMPTY_LOG_MAX_BYTES and not any(
        _is_user_turn(line) for line in head.split(b"\n") if _USER_TURN_MARK in line
    ):
        return True
    if len(_HAS_TURN) >= _CACHE_MAX:
        _HAS_TURN.clear()
    _HAS_TURN.add(log)
    return False


def _is_user_turn(line: bytes) -> bool:
    try:
        return json.loads(line).get("type") == "user.message"
    except (ValueError, AttributeError):
        return False


def _lock_holders(folder: Path, since: float) -> Dict[int, float]:
    """``{pid: opened at}`` for the live processes that opened ``folder`` at or
    after ``since``. A lock a crashed process left behind names nobody."""
    holders: Dict[int, float] = {}
    try:
        names = os.listdir(folder)
    except OSError:
        return holders
    for name in names:
        match = _LOCK_RE.match(name)
        if match is None:
            continue
        pid = int(match.group(1))
        try:
            opened = (folder / name).stat().st_mtime
        except OSError:
            continue
        if opened >= since and psutil.pid_exists(pid):
            holders[pid] = opened
    return holders


def reopened_in(folder: Path, since: float, until: float) -> bool:
    """Whether this folder was opened again between those instants: a live
    process took its lock, which is all there is before the first prompt, or
    its log records a ``session.resume``. Only a log written since ``since``
    is read for one."""
    events = folder / "events.jsonl"
    try:
        written = events.stat().st_mtime
    except OSError:
        return False
    if any(opened <= until for opened in _lock_holders(folder, since).values()):
        return True
    return written >= since and any(since <= t <= until for t in _resume_times(events))


def shown_folder(state_dir: Path, launch: Path, since: float, until: float) -> Optional[Path]:
    """The folder the process that created ``launch`` opened last, when that is
    another one; else None.

    The launching process is the one holding ``launch`` since between
    ``since`` and ``until``; with none or several, nothing is followed. A
    result is kept for :data:`SHOWN_TTL_S`.
    """
    key = (state_dir, launch)
    now = time.time()
    hit = _SHOWN_CACHE.get(key)
    if hit is not None and now - hit[0] < SHOWN_TTL_S:
        return hit[1]
    found = _scan(state_dir, launch, since, until)
    if len(_SHOWN_CACHE) >= _CACHE_MAX:
        _SHOWN_CACHE.clear()
    _SHOWN_CACHE[key] = (now, found)
    return found


def _scan(state_dir: Path, launch: Path, since: float, until: float) -> Optional[Path]:
    owners = [(pid, at) for pid, at in _lock_holders(launch, since).items() if at <= until]
    if len(owners) != 1:
        return None
    pid, newest = owners[0]
    found: Optional[Path] = None
    try:
        for lock in state_dir.glob(f"*/inuse.{pid}.lock"):
            opened = lock.stat().st_mtime
            if lock.parent != launch and opened > newest:
                newest, found = opened, lock.parent
    except OSError:
        return None
    return found


def _resume_times(events: Path) -> List[float]:
    """Every ``session.resume`` instant in one event log, as epoch seconds.

    The log only grows, so each file is read once from the offset the last
    call reached: the live tick asks on every poll while a session stays
    unresolved, and a long session's log runs to hundreds of MB. A shrunk
    file was replaced and is read again from the start.
    """
    try:
        size = events.stat().st_size
    except OSError:
        return []
    offset, times = _RESUME_CACHE.get(events, (0, []))
    if size < offset:
        offset, times = 0, []
    times = list(times)
    try:
        with events.open("rb") as fh:
            fh.seek(offset)
            carry = b""
            while True:
                chunk = fh.read(_RESUME_CHUNK_BYTES)
                if not chunk:
                    break
                lines = (carry + chunk).split(b"\n")
                carry = lines.pop()
                offset += len(chunk)
                for line in lines:
                    if _RESUME_MARK in line:
                        stamp = _resume_instant(line)
                        if stamp is not None:
                            times.append(stamp)
            # An unfinished last line is read again once it is complete.
            offset -= len(carry)
    except OSError:
        return times
    if len(_RESUME_CACHE) >= _CACHE_MAX:
        _RESUME_CACHE.clear()
    _RESUME_CACHE[events] = (offset, times)
    return times


def _resume_instant(line: bytes) -> Optional[float]:
    """One ``session.resume`` line's timestamp as epoch seconds, or None."""
    try:
        record = json.loads(line)
        if record.get("type") != "session.resume":
            return None
        return datetime.fromisoformat(
            str(record["timestamp"]).replace("Z", "+00:00")
        ).timestamp()
    except (ValueError, KeyError, TypeError, AttributeError, OSError):
        return None
