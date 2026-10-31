"""Name the process behind a PC-health warning, and restart it if allowlisted.

The hub's Watch/Act verdict names a process in its ``fix`` ("Restart
hwinfo-plugin.exe"). :func:`annotate` adds who owns that process (``owner``) and,
when it is on :data:`TARGETS`, a ``restart_id`` the sheet's Restart button posts.

The allowlist lives in code, not in the editable config: it is the whole safety
case for stopping a process from a phone. A target matches only by exact image
name *and* the folder the executable runs from, and the id is resolved to pids
here, server side. The client never sends a pid or a name.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Any, Dict, List, Optional, Tuple

import psutil

_log = logging.getLogger(__name__)

# How long a stopped process gets to exit before it is killed outright.
_STOP_WAIT_SECONDS = 3.0

_EXE_NAME = re.compile(r"[\w.\-]+\.exe", re.IGNORECASE)
_RESTART_FIX = re.compile(r"^restart\s+([\w.\-]+\.exe)\b", re.IGNORECASE)


class RestartError(Exception):
    """A restart that was refused or could not finish; ``status`` is the HTTP code."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class RestartTarget:
    id: str
    owner: str
    process: str  # exact image name, compared case-insensitively
    folder: Tuple[str, ...]  # trailing folder names the executable must sit in
    also_stop: Tuple[str, ...] = ()  # parent image names stopped with it (same folder)


TARGETS: Tuple[RestartTarget, ...] = (
    RestartTarget(
        id="streamdeck-hwinfo",
        owner="Stream Deck · HWiNFO plugin",
        process="hwinfo-plugin.exe",
        folder=("elgato", "streamdeck", "plugins", "com.exension.hwinfo.sdplugin"),
        # Stream Deck relaunches its plugin once the host process is gone.
        also_stop=("hwinfo.exe",),
    ),
)


def _folder(exe: str) -> Tuple[str, ...]:
    return tuple(part.lower() for part in PureWindowsPath(exe).parent.parts)


def _in_folder(exe: str, folder: Tuple[str, ...]) -> bool:
    got = _folder(exe)
    return len(got) >= len(folder) and got[len(got) - len(folder):] == folder


def get_target(restart_id: str) -> Optional[RestartTarget]:
    return next((t for t in TARGETS if t.id == restart_id), None)


def _matches(target: RestartTarget, name: str, exe: str) -> bool:
    return name.lower() == target.process and bool(exe) and _in_folder(exe, target.folder)


def _live(name: str) -> List[psutil.Process]:
    """Running processes whose image name is *name*; those that vanish or deny a read are skipped."""
    found: List[psutil.Process] = []
    for proc in psutil.process_iter(["name"]):
        if (proc.info.get("name") or "").lower() == name.lower():
            found.append(proc)
    return found


def _exe(proc: psutil.Process) -> str:
    try:
        return proc.exe() or ""
    except (psutil.Error, OSError):
        return ""


def process_name_in(health: Dict[str, Any]) -> Optional[str]:
    """The image name the hub's verdict points at: its ``fix`` first, else its ``reason``."""
    fix = _RESTART_FIX.match(health.get("fix") or "")
    if fix:
        return fix.group(1)
    found = _EXE_NAME.search(health.get("reason") or "")
    return found.group(0) if found else None


def _owner_from_path(exe: str) -> Optional[str]:
    """The vendor folder under Program Files, for a process no target names."""
    parts = PureWindowsPath(exe).parts
    for i, part in enumerate(parts[:-1]):
        if part.lower() in ("program files", "program files (x86)") and i + 1 < len(parts) - 1:
            return parts[i + 1]
    return None


def annotate(health: Dict[str, Any]) -> Dict[str, Any]:
    """*health* with ``owner`` and ``restart_id`` (both None when not known).

    Only a Watch/Act verdict naming a running process gets either. Blocking: it scans
    the process table.
    """
    out = dict(health, owner=None, restart_id=None)
    if health.get("verdict") not in ("watch", "act"):
        return out
    name = process_name_in(health)
    if not name:
        return out
    for proc in _live(name):
        exe = _exe(proc)
        target = next((t for t in TARGETS if _matches(t, name, exe)), None)
        if target:
            out["owner"], out["restart_id"] = target.owner, target.id
            return out
        out["owner"] = out["owner"] or _owner_from_path(exe)
    return out


def _stop(proc: psutil.Process) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=_STOP_WAIT_SECONDS)
    except psutil.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=_STOP_WAIT_SECONDS)


def restart(restart_id: str) -> Dict[str, Any]:
    """Stop the allowlisted process behind *restart_id*; its supervisor relaunches it.

    Refuses an id that is not on :data:`TARGETS` (404), a target that is not running
    (409), and reports a process that would not stop (500). Blocking.
    """
    target = get_target(restart_id)
    if target is None:
        raise RestartError(f"'{restart_id}' is not on the restart allowlist", 404)
    victims: Dict[int, psutil.Process] = {}
    for proc in _live(target.process):
        if not _matches(target, target.process, _exe(proc)):
            continue
        victims[proc.pid] = proc
        try:
            parent = proc.parent()
        except (psutil.Error, OSError):
            parent = None
        if parent is not None and parent.name().lower() in target.also_stop and _in_folder(_exe(parent), target.folder):
            victims[parent.pid] = parent
    if not victims:
        raise RestartError(f"{target.process} is not running, so there is nothing to restart", 409)
    stopped: List[str] = []
    errors: List[str] = []
    for pid, proc in victims.items():
        try:
            name = proc.name()
            _stop(proc)
            stopped.append(name)
        except psutil.NoSuchProcess:
            continue
        except (psutil.Error, OSError) as exc:
            errors.append(f"pid {pid}: {exc}")
    _log.info("ℹ️ restart %s: stopped %s, %d error(s)", target.id, stopped, len(errors))
    if errors and not stopped:
        raise RestartError("Could not stop " + target.process + ": " + "; ".join(errors), 500)
    return {"restart_id": target.id, "owner": target.owner, "stopped": stopped, "errors": errors}
