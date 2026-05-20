"""In-process tracker for apps spawned by the launcher.

The Apps tab needs a "what did I launch?" list the way the Claude Code
tab has one for sessions. Claude sessions are owned by the session-host;
plain ``*.bat`` apps are not — they run in their own CMD windows. This
module keeps a small in-memory record of every bat the launcher spawned
so ``/api/apps/running`` can list them, bind each to its listening port,
and offer a per-instance Stop.

State lives in process memory only: a webapp restart forgets it. That
matches the rest of the tray's behaviour — orphaned listeners still show
in the Port listeners panel.

Not routed through the session-host: that host is for ``claude`` PTYs
only. This stays a plain module-level structure.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import List

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover — psutil is in requirements.txt
    psutil = None  # type: ignore

logger = logging.getLogger(__name__)

# How far a process's reported create_time may drift from the recorded
# started_at before we treat the PID as reused (Windows recycles PIDs).
_CREATE_TIME_TOLERANCE_SECONDS = 1.0


@dataclass
class SpawnedInstance:
    """One launcher-spawned app process."""

    app_id: str
    name: str
    kind: str
    pid: int
    started_at: float  # epoch seconds


# (app_id, pid) → SpawnedInstance. Keyed on the pair because the same
# app_id may be launched multiple times, each with a distinct PID.
_instances: dict[tuple[str, int], SpawnedInstance] = {}
_lock = threading.Lock()


def record_spawn(app_id: str, name: str, kind: str, pid: int) -> SpawnedInstance:
    """Register a freshly-spawned app process and return its record."""
    inst = SpawnedInstance(
        app_id=app_id, name=name, kind=kind, pid=pid, started_at=time.time()
    )
    with _lock:
        _instances[(app_id, pid)] = inst
    logger.info(f"📒 tracking spawned app {name!r} (id={app_id}, pid={pid})")
    return inst


def forget_spawn(app_id: str, pid: int) -> None:
    """Drop the record for ``(app_id, pid)`` — safe if none was tracked."""
    with _lock:
        _instances.pop((app_id, pid), None)


def list_running() -> List[SpawnedInstance]:
    """Return every currently-tracked instance (a snapshot copy)."""
    with _lock:
        return list(_instances.values())


def is_tracked(app_id: str, pid: int) -> bool:
    """True when ``(app_id, pid)`` is currently tracked."""
    with _lock:
        return (app_id, pid) in _instances


def _is_alive(inst: SpawnedInstance) -> bool:
    """True when the PID still exists and is the same process we recorded.

    The ``create_time`` check guards against Windows PID reuse: a recycled
    PID would exist but have a creation time far from ``started_at``.
    """
    if psutil is None:
        return True  # can't introspect — assume alive, don't drop blindly
    try:
        if not psutil.pid_exists(inst.pid):
            return False
        proc = psutil.Process(inst.pid)
        if not proc.is_running():
            return False
        try:
            create_time = proc.create_time()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            return True  # exists but unreadable — keep it
        return abs(create_time - inst.started_at) <= _CREATE_TIME_TOLERANCE_SECONDS
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return False


def prune_dead() -> None:
    """Drop every instance whose process has exited (or been PID-reused)."""
    with _lock:
        dead = [key for key, inst in _instances.items() if not _is_alive(inst)]
        for key in dead:
            inst = _instances.pop(key)
            logger.info(
                f"🧹 pruned dead app instance {inst.name!r} "
                f"(id={inst.app_id}, pid={inst.pid})"
            )
