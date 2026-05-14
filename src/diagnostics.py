"""Runtime diagnostics — log capture and port-owner introspection.

Two pieces, both pure data (no UI imports):

- ``RingLogHandler`` keeps the last N formatted Python logging lines in
  memory so a UI surface can show what the app has been doing without
  parsing files. Attached once to the root logger from ``cli.main``.

- ``port_owner`` answers "who is actually serving on port N?" using
  psutil. Used by the smart-kill endpoint.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, List, Optional

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover — psutil is in requirements.txt
    psutil = None  # type: ignore

logger = logging.getLogger(__name__)

DEFAULT_RING_CAPACITY = 500


# --------------------------------------------------------------------- logging


class RingLogHandler(logging.Handler):
    """Thread-safe in-memory ring buffer of formatted log lines."""

    def __init__(self, capacity: int = DEFAULT_RING_CAPACITY) -> None:
        super().__init__()
        self._buffer: Deque[str] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:  # noqa: BLE001 — logging contract: never raise
            self.handleError(record)
            return
        with self._lock:
            self._buffer.append(line)

    def lines(self) -> List[str]:
        with self._lock:
            return list(self._buffer)


_handler_lock = threading.Lock()
_handler: Optional[RingLogHandler] = None


def app_log_handler() -> RingLogHandler:
    """Return the singleton handler, creating it on first call."""
    global _handler
    with _handler_lock:
        if _handler is None:
            h = RingLogHandler()
            h.setFormatter(
                logging.Formatter(
                    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%H:%M:%S",
                )
            )
            _handler = h
        return _handler


def attach_app_log_handler() -> None:
    """Idempotently attach the ring handler to the root logger."""
    h = app_log_handler()
    root = logging.getLogger()
    if h not in root.handlers:
        root.addHandler(h)


# ------------------------------------------------------------- port introspection


@dataclass
class PortOwner:
    pid: int
    port: int
    name: str = ""
    exe: str = ""
    cwd: str = ""
    cmdline: List[str] = field(default_factory=list)

    def cmdline_str(self) -> str:
        return " ".join(self.cmdline) if self.cmdline else ""


def port_owner(port: int) -> Optional[PortOwner]:
    """Best-effort lookup of the process LISTENing on ``port``.

    Returns ``None`` when psutil is unavailable, the lookup is denied,
    or no listener is found.
    """
    if psutil is None:
        return None

    try:
        connections = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, PermissionError, OSError) as exc:
        logger.debug(f"port_owner: net_connections denied ({exc})")
        return None

    for conn in connections:
        try:
            if not conn.laddr or conn.laddr.port != port:
                continue
            if conn.status != psutil.CONN_LISTEN:
                continue
            if conn.pid is None:
                continue
        except AttributeError:
            continue

        owner = PortOwner(pid=int(conn.pid), port=port)
        try:
            proc = psutil.Process(conn.pid)
            owner.name = proc.name() or ""
            try:
                owner.exe = proc.exe() or ""
            except (psutil.AccessDenied, FileNotFoundError):
                owner.exe = ""
            try:
                owner.cmdline = list(proc.cmdline() or [])
            except (psutil.AccessDenied, FileNotFoundError):
                owner.cmdline = []
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

        return owner

    return None


def find_pids_on_port(port: int) -> List[int]:
    """Return all PIDs LISTENing on ``port`` (smart-kill helper)."""
    if psutil is None:
        return []
    own_pid = os.getpid()
    pids: set[int] = set()
    try:
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                for conn in proc.net_connections(kind="inet"):
                    if (
                        conn.status == psutil.CONN_LISTEN
                        and conn.laddr
                        and conn.laddr.port == port
                    ):
                        pid = proc.info["pid"]
                        if pid and pid != own_pid:
                            pids.add(pid)
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
    except (psutil.AccessDenied, PermissionError, OSError) as exc:
        logger.debug(f"find_pids_on_port denied ({exc})")
        return []
    return sorted(pids)


# Process names that count as "an app server worth offering to kill".
# Streamlit auto-increments its port past 8501, so a fixed port list
# misses every app after the first — discover by process instead.
_APP_PROC_HINTS = ("python", "pythonw", "streamlit")


def list_app_listeners() -> List[PortOwner]:
    """Enumerate every LISTEN socket owned by an app-server-like process.

    Unlike :func:`port_owner` (one fixed port) this walks all listeners
    and keeps the ones whose owning process looks like a Streamlit /
    FastAPI app, so the smart-kill panel finds apps wherever they bound.
    """
    if psutil is None:
        return []
    own_pid = os.getpid()
    found: dict[int, PortOwner] = {}
    try:
        connections = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, PermissionError, OSError) as exc:
        logger.debug(f"list_app_listeners: net_connections denied ({exc})")
        return []

    for conn in connections:
        try:
            if conn.status != psutil.CONN_LISTEN:
                continue
            if not conn.laddr or conn.pid is None:
                continue
        except AttributeError:
            continue
        port = conn.laddr.port
        if port in found or conn.pid == own_pid:
            continue
        try:
            proc = psutil.Process(conn.pid)
            pname = proc.name() or ""
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if not any(hint in pname.lower() for hint in _APP_PROC_HINTS):
            continue

        owner = PortOwner(pid=int(conn.pid), port=port, name=pname)
        try:
            owner.exe = proc.exe() or ""
        except (psutil.AccessDenied, FileNotFoundError):
            owner.exe = ""
        try:
            owner.cwd = proc.cwd() or ""
        except (psutil.AccessDenied, FileNotFoundError):
            owner.cwd = ""
        try:
            owner.cmdline = list(proc.cmdline() or [])
        except (psutil.AccessDenied, FileNotFoundError):
            owner.cmdline = []
        found[port] = owner

    return [found[p] for p in sorted(found)]


def kill_pids(pids: List[int]) -> tuple[List[int], List[str]]:
    """Force-kill ``pids``. Returns ``(killed_pids, error_messages)``."""
    if psutil is None:
        return [], ["psutil not available"]
    killed: List[int] = []
    errors: List[str] = []
    for pid in pids:
        try:
            proc = psutil.Process(pid)
            proc.kill()
            try:
                proc.wait(timeout=3)
            except psutil.TimeoutExpired:
                pass
            killed.append(pid)
        except psutil.NoSuchProcess:
            killed.append(pid)
        except (psutil.AccessDenied, OSError) as exc:
            errors.append(f"PID {pid}: {exc}")
    return killed, errors
