"""Name the process behind a PC-health warning, and restart it if allowlisted.

The hub's Watch/Act verdict names a process in its ``fix`` ("Restart
hwinfo-plugin.exe"). :func:`annotate` adds who owns that process (``owner``) and,
when it is on :data:`TARGETS`, a ``restart_id`` the sheet's Restart button posts.

The allowlist lives in code, not in the editable config: it is the whole safety
case for stopping a process from a phone. A target matches only by exact image
name *and* the folder the executable runs from, and the id is resolved to pids
here, server side. The client never sends a pid or a name.

A target with ``relaunch`` has no supervisor (Wave Link), so the restart starts it
again from the executable path read off the process it just stopped, with the
target's own ``relaunch_args`` (Wave Link: ``-runinbk``, so it comes back in the
background with no window), and says whether it came back as its own state:
``supervisor``, ``launched`` or ``not confirmed``, never folded into success.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Any, Callable, Dict, List, Optional, Tuple

import psutil

from src.subprocess_flags import NO_WINDOW

_log = logging.getLogger(__name__)

# How long a stopped process gets to exit before it is killed outright.
_STOP_WAIT_SECONDS = 3.0
# After the stop, how long a vendor's own watcher gets to bring the app back before
# this module starts it, and how long a started app gets to show up.
_SUPERVISOR_GRACE_SECONDS = 5.0
_RELAUNCH_WAIT_SECONDS = 20.0
_POLL_SECONDS = 0.5

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
    relaunch: bool = False  # no supervisor: start it again after the stop
    # Fixed in code, never from the client or the stopped process's own command line:
    # a process started by hand lacks the flag, and reading it back would keep it lost.
    relaunch_args: Tuple[str, ...] = ()


TARGETS: Tuple[RestartTarget, ...] = (
    RestartTarget(
        id="streamdeck-hwinfo",
        owner="Stream Deck · HWiNFO plugin",
        process="hwinfo-plugin.exe",
        folder=("elgato", "streamdeck", "plugins", "com.exension.hwinfo.sdplugin"),
        # Stream Deck relaunches its plugin once the host process is gone.
        also_stop=("hwinfo.exe",),
    ),
    RestartTarget(
        id="wavelink",
        owner="Elgato Wave Link",
        process="wavelink.exe",
        folder=("elgato", "wavelink"),
        relaunch=True,
        relaunch_args=("-runinbk",),  # run in the background, no window (night restarts are unattended)
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


def _victims(target: RestartTarget) -> Tuple[Dict[int, psutil.Process], str]:
    """The processes a restart of *target* stops, and the exe path of the first match."""
    victims: Dict[int, psutil.Process] = {}
    exe_path = ""
    for proc in _live(target.process):
        exe = _exe(proc)
        if not _matches(target, target.process, exe):
            continue
        exe_path = exe_path or exe
        victims[proc.pid] = proc
        try:
            parent = proc.parent()
        except (psutil.Error, OSError):
            parent = None
        if parent is not None and parent.name().lower() in target.also_stop and _in_folder(_exe(parent), target.folder):
            victims[parent.pid] = parent
    return victims, exe_path


def _wait_for(ready: Callable[[], bool], seconds: float) -> bool:
    """Poll *ready* until it is true or *seconds* pass."""
    deadline = time.monotonic() + seconds
    while True:
        if ready():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_SECONDS)


def _launch(exe: str, args: Tuple[str, ...] = ()) -> None:
    """Start *exe* with *args*, re-parented off the webapp.

    ``cmd /c start`` hands the app to Windows and exits, so a later kill of the
    webapp's process tree (a tray restart) cannot take the app with it.
    """
    subprocess.Popen(  # noqa: S603 — path read off the allowlisted process, args fixed in code, none from the client
        [os.environ.get("COMSPEC", "cmd.exe"), "/c", "start", "", exe, *args],
        cwd=str(PureWindowsPath(exe).parent), creationflags=NO_WINDOW, close_fds=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _relaunch(target: RestartTarget, exe: str, old_pids: set) -> str:
    """Bring *target* back after its stop and say how: ``supervisor`` when a vendor
    watcher did it within the grace, ``launched`` when this module did and the app
    showed up, else ``not confirmed``."""
    def back() -> bool:
        return any(p.pid not in old_pids and _matches(target, target.process, _exe(p)) for p in _live(target.process))

    if _wait_for(back, _SUPERVISOR_GRACE_SECONDS):
        return "supervisor"
    try:
        _launch(exe, target.relaunch_args)
    except OSError as exc:
        _log.warning("⚠️ restart %s: could not start %s: %s", target.id, exe, exc)
        return "not confirmed"
    return "launched" if _wait_for(back, _RELAUNCH_WAIT_SECONDS) else "not confirmed"


def restart(restart_id: str) -> Dict[str, Any]:
    """Stop the allowlisted process behind *restart_id* and make sure it is back.

    A supervised target is relaunched by its supervisor; a ``relaunch`` target is
    started again here (see :func:`_relaunch`), and the result's ``relaunch`` says
    whether it was seen again. Refuses an id that is not on :data:`TARGETS` (404), a
    target that is not running (409), and reports a process that would not stop
    (500). Blocking.
    """
    target = get_target(restart_id)
    if target is None:
        raise RestartError(f"'{restart_id}' is not on the restart allowlist", 404)
    victims, exe_path = _victims(target)
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
    outcome = _relaunch(target, exe_path, set(victims)) if target.relaunch else None
    if outcome:
        _log.info("ℹ️ restart %s: relaunch %s", target.id, outcome)
    return {
        "restart_id": target.id, "owner": target.owner, "stopped": stopped,
        "errors": errors, "relaunch": outcome,
    }
