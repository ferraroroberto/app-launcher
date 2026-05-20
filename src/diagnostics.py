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
import socket
import ssl
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


def listening_port_for_pid_tree(root_pid: int) -> Optional[int]:
    """Return the first port any process in ``root_pid``'s tree LISTENs on.

    Walks ``root_pid`` itself plus every descendant
    (``psutil.Process.children(recursive=True)``) — a launcher-spawned bat
    is typically ``cmd.exe`` → ``cmd.exe`` wrapper → ``python.exe``, and the
    socket belongs to the leaf python. Returns ``None`` when psutil is
    unavailable, the tree is gone, or nothing in it is listening.
    """
    if psutil is None:
        return None
    try:
        root = psutil.Process(root_pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None

    procs = [root]
    try:
        procs.extend(root.children(recursive=True))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    for proc in procs:
        try:
            for conn in proc.net_connections(kind="inet"):
                if (
                    conn.status == psutil.CONN_LISTEN
                    and conn.laddr
                    and conn.laddr.port
                ):
                    return int(conn.laddr.port)
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return None


def detect_local_scheme(port: int, timeout: float = 1.0) -> str:
    """Probe loopback ``port`` and report ``"https"`` or ``"http"``.

    Apps in this ecosystem are split: the FastAPI siblings serve HTTPS,
    a default Streamlit server serves plain HTTP. We can't hard-code
    either — a wrong scheme makes the phone's browser fail the TLS
    handshake. So we attempt a TLS handshake against ``127.0.0.1:port``:
    it completing means HTTPS, failing means HTTP. Falls back to
    ``"http"`` when the port can't be reached on loopback at all.
    """
    ctx = ssl._create_unverified_context()
    try:
        with socket.create_connection(
            ("127.0.0.1", port), timeout=timeout
        ) as sock:
            with ctx.wrap_socket(sock, server_hostname="127.0.0.1"):
                return "https"
    except (ssl.SSLError, OSError):
        return "http"


# Process names that count as "an app server worth offering to kill".
# Streamlit auto-increments its port past 8501, so a fixed port list
# misses every app after the first — discover by process instead.
_APP_PROC_HINTS = ("python", "pythonw", "streamlit")

# IANA dynamic/private range — anything ≥ this on loopback is an internal
# socket (pywinpty opens one per PTY spawn), not a user-launchable app.
_EPHEMERAL_PORT_MIN = 49152
_LOOPBACK_PREFIXES = ("127.", "::1")


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
        # Skip loopback ephemeral listeners — pywinpty opens one per
        # PtyProcess.spawn() and they linger after the session ends.
        ip = getattr(conn.laddr, "ip", "") or ""
        if port >= _EPHEMERAL_PORT_MIN and ip.startswith(_LOOPBACK_PREFIXES):
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


def kill_process_tree(pid: int) -> List[int]:
    """Kill ``pid`` and every descendant; return the PIDs that were killed.

    Sends ``terminate()`` to the whole tree, waits up to 3 s for a clean
    exit, then ``kill()``s whatever is still alive. A missing process
    counts as already-killed. Used by the per-instance Stop endpoint.
    """
    if psutil is None:
        return []
    try:
        root = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return [pid]

    procs = [root]
    try:
        procs.extend(root.children(recursive=True))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    for proc in procs:
        try:
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    gone, alive = psutil.wait_procs(procs, timeout=3)
    for proc in alive:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    return [p.pid for p in procs]


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
