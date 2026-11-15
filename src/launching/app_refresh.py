"""One-tap refresh of a registered app: stop what the launcher tracks, relaunch hidden.

The pieces the ``POST /api/apps/{id}/refresh`` route composes. A stop is only
reported as done once the process is gone, never when the kill was merely sent,
and the two things a refresh must never touch (the launcher itself and the
session-host) are refused here, before anything is stopped.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

from src import app_runtime
from src.diagnostics import kill_process_tree, listening_port_for_pid_tree
from src.instance_role import PROJECT_ROOT

logger = logging.getLogger(__name__)

# How long a killed tree gets to disappear from the process table before the
# stop is reported as failed. kill_process_tree already waits for a clean exit
# and then kills, so this only covers the table catching up.
STOP_WAIT_SECONDS = 8.0
_POLL_SECONDS = 0.25


@dataclass(frozen=True)
class StopResult:
    pid: int
    stopped: bool


def is_launcher_bat(bat_path: Path) -> bool:
    """True for a bat that starts this launcher: this checkout's, or any other
    checkout of the same repo (the primary one a worktree webapp is a copy of).

    The session-host package is the one marker only this repo has, so a
    worktree and the primary are both recognised without naming either.
    """
    try:
        folder = bat_path.resolve().parent
    except OSError:
        return False
    return folder == PROJECT_ROOT.resolve() or (
        folder / "app" / "webapp" / "session_host"
    ).is_dir()


def protected_port_in_use(
    instances: Iterable[app_runtime.SpawnedInstance], protected_ports: Sequence[int]
) -> Optional[int]:
    """The first protected port a tracked instance's process tree listens on."""
    for inst in instances:
        port = listening_port_for_pid_tree(inst.pid)
        if port is not None and port in protected_ports:
            return port
    return None


def stop_instance(inst: app_runtime.SpawnedInstance) -> StopResult:
    """Kill one tracked instance's tree and wait until it is really gone."""
    kill_process_tree(inst.pid)
    deadline = time.monotonic() + STOP_WAIT_SECONDS
    while True:
        # prune_dead drops every record whose process (PID-reuse guarded) has
        # exited, so "no longer tracked" is the same fact the Running panel shows.
        app_runtime.prune_dead()
        if not app_runtime.is_tracked(inst.app_id, inst.pid):
            logger.info(f"🛑 refresh: stopped {inst.name!r} pid={inst.pid}")
            return StopResult(inst.pid, True)
        if time.monotonic() >= deadline:
            logger.warning(
                f"⚠️ refresh: {inst.name!r} pid={inst.pid} still running "
                f"{STOP_WAIT_SECONDS:.0f}s after the stop"
            )
            return StopResult(inst.pid, False)
        time.sleep(_POLL_SECONDS)
