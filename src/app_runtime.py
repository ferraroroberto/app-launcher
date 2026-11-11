"""Tracker for apps spawned by the launcher, persisted across launcher restarts.

The Apps tab needs a "what did I launch?" list the way the Claude Code
tab has one for sessions. Claude sessions are owned by the session-host;
plain ``*.bat`` apps are not — they run in their own CMD windows. This
module keeps a record of every bat the launcher spawned so
``/api/apps/running`` can list them, bind each to its listening port,
and offer a per-instance Stop.

A launched app is re-parented out of the launcher's process tree
(:func:`src.launching.launcher.spawn_bat`), so it outlives a launcher
restart. The records are written to ``spawned-apps.json`` beside the audit
files, and a fresh launcher re-adopts each one whose process is still the
one recorded — and logs, by name, each one that is gone, so an app that
died across a restart is visible in the launcher log.

Not routed through the session-host: that host is for ``claude`` PTYs
only. This stays a plain module-level structure.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional

from src import runtime_files
from src._json_io import atomic_write_json
from src.audit import AUDIT_DIR_ENV
from src.diagnostics import is_pid_alive

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover — psutil is in requirements.txt
    psutil = None  # type: ignore

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# How far a process's reported create_time may drift from the recorded
# started_at before we treat the PID as reused (Windows recycles PIDs).
_CREATE_TIME_TOLERANCE_SECONDS = 1.0


def _resolve_state_file() -> Path:
    # Follows the audit files wherever LAUNCHER_AUDIT_DIR sends them, so a
    # test or gate webapp never reads or rewrites the live launcher's records.
    override = os.environ.get(AUDIT_DIR_ENV, "").strip()
    return (Path(override) if override else PROJECT_ROOT / "webapp") / "spawned-apps.json"


STATE_FILE = _resolve_state_file()


@dataclass
class SpawnedInstance:
    """One launcher-spawned app process."""

    app_id: str
    name: str
    kind: str
    pid: int
    started_at: float  # epoch seconds: the process's own create time when readable


# (app_id, pid) → SpawnedInstance. Keyed on the pair because the same
# app_id may be launched multiple times, each with a distinct PID.
_instances: dict[tuple[str, int], SpawnedInstance] = {}
_lock = threading.Lock()
_loaded = False


def find_child_process(
    parent_pid: int, name: str, created_after: float, tolerance: float = 1.0
) -> Optional[int]:
    """PID of the ``name`` process ``parent_pid`` created at or after ``created_after``.

    Works after the parent has exited: Windows keeps a process's parent PID
    on record, which is how a child re-parented through ``cmd /c start`` is
    found again. The create-time floor (minus ``tolerance``) rules out an
    older process that happens to name a since-reused PID as its parent.
    ``None`` when psutil is unavailable or no such child exists.
    """
    if psutil is None:
        return None
    floor = created_after - tolerance
    for proc in psutil.process_iter(["pid", "ppid", "name", "create_time"]):
        info = proc.info
        if (
            info.get("ppid") == parent_pid
            and str(info.get("name") or "").lower() == name.lower()
            and (info.get("create_time") or 0.0) >= floor
        ):
            return int(info["pid"])
    return None


def _create_time(pid: int) -> Optional[float]:
    if psutil is None:
        return None
    try:
        return float(psutil.Process(pid).create_time())
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError, OSError):
        return None


def _save_locked() -> None:
    """Write every record to :data:`STATE_FILE`; caller holds ``_lock``."""
    payload = [asdict(inst) for inst in _instances.values()]
    try:
        runtime_files.make_dirs(STATE_FILE.parent)
        atomic_write_json(STATE_FILE, payload)
    except (OSError, ValueError) as exc:
        logger.warning(f"⚠️ could not save launched-app records to {STATE_FILE}: {exc}")


def _read_state() -> List[SpawnedInstance]:
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        logger.warning(f"⚠️ launched-app records at {STATE_FILE} unreadable, starting empty: {exc}")
        return []
    records: List[SpawnedInstance] = []
    for row in raw if isinstance(raw, list) else []:
        try:
            records.append(SpawnedInstance(
                app_id=str(row["app_id"]), name=str(row["name"]), kind=str(row["kind"]),
                pid=int(row["pid"]), started_at=float(row["started_at"]),
            ))
        except (KeyError, TypeError, ValueError):
            logger.warning(f"⚠️ skipped a malformed launched-app record: {row!r}")
    return records


def _ensure_loaded_locked() -> None:
    """Re-adopt the records a previous launcher left; caller holds ``_lock``."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    adopted: List[str] = []
    gone: List[str] = []
    for inst in _read_state():
        label = f"{inst.name!r} (id={inst.app_id}, pid={inst.pid})"
        if _is_alive(inst):
            _instances[(inst.app_id, inst.pid)] = inst
            adopted.append(label)
        else:
            gone.append(label)
    if adopted:
        logger.info(f"🔗 re-adopted {len(adopted)} launched app(s) still running: {', '.join(adopted)}")
    if gone:
        logger.warning(
            f"⚠️ {len(gone)} launched app(s) recorded by the previous launcher are no longer "
            f"running (stopped while the launcher was down): {', '.join(gone)}"
        )
        _save_locked()


def adopt_previous() -> None:
    """Re-adopt the previous launcher's still-running apps now, at boot, so the
    adopted / gone log lines land at restart time rather than on the first poll."""
    with _lock:
        _ensure_loaded_locked()


def record_spawn(app_id: str, name: str, kind: str, pid: int) -> SpawnedInstance:
    """Register a freshly-spawned app process and return its record."""
    started_at = _create_time(pid) or time.time()
    inst = SpawnedInstance(
        app_id=app_id, name=name, kind=kind, pid=pid, started_at=started_at
    )
    with _lock:
        _ensure_loaded_locked()
        _instances[(app_id, pid)] = inst
        _save_locked()
    logger.info(f"📒 tracking spawned app {name!r} (id={app_id}, pid={pid})")
    return inst


def forget_spawn(app_id: str, pid: int) -> None:
    """Drop the record for ``(app_id, pid)`` — safe if none was tracked."""
    with _lock:
        _ensure_loaded_locked()
        if _instances.pop((app_id, pid), None) is not None:
            _save_locked()


def list_running() -> List[SpawnedInstance]:
    """Return every currently-tracked instance (a snapshot copy)."""
    with _lock:
        _ensure_loaded_locked()
        return list(_instances.values())


def is_tracked(app_id: str, pid: int) -> bool:
    """True when ``(app_id, pid)`` is currently tracked."""
    with _lock:
        _ensure_loaded_locked()
        return (app_id, pid) in _instances


def _is_alive(inst: SpawnedInstance) -> bool:
    """True when the PID still exists and is the same process we recorded.

    The ``create_time`` check guards against Windows PID reuse: a recycled
    PID would exist but have a creation time far from ``started_at``. That
    guard is a real correctness fix, so it lives in exactly one place —
    :func:`src.diagnostics.is_pid_alive`, which ``src.jobs.reap`` calls for
    the same reason on run records. This is the spawned-app
    binding of it: the recorded ``started_at`` is the create-time hint.
    """
    return is_pid_alive(
        inst.pid, inst.started_at, _CREATE_TIME_TOLERANCE_SECONDS
    )


def prune_dead() -> None:
    """Drop every instance whose process has exited (or been PID-reused)."""
    with _lock:
        _ensure_loaded_locked()
        dead = [key for key, inst in _instances.items() if not _is_alive(inst)]
        for key in dead:
            inst = _instances.pop(key)
            logger.info(
                f"🧹 pruned dead app instance {inst.name!r} "
                f"(id={inst.app_id}, pid={inst.pid})"
            )
        if dead:
            _save_locked()
