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
from typing import Callable, Deque, List, Optional

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
    # PID of the nearest ancestor that is *itself* a listed listener, or
    # ``None`` for a top-level app. Set by :func:`_assign_parents` so the
    # UI can nest a helper service (e.g. a TTS shim a hub spawned) under
    # its parent app instead of showing it as a duplicate top-level row.
    parent_pid: Optional[int] = None

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

    owners = [found[p] for p in sorted(found)]
    _assign_parents(owners, _psutil_ppid)
    return owners


def _psutil_ppid(pid: int) -> Optional[int]:
    """Parent PID of ``pid`` via psutil, or ``None`` if unknown/denied."""
    if psutil is None:
        return None
    try:
        return int(psutil.Process(pid).ppid())
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return None


def _assign_parents(
    owners: List[PortOwner],
    ppid_lookup: Callable[[int], Optional[int]],
) -> None:
    """Set each owner's ``parent_pid`` to its parent listener, or leave it
    ``None`` for a top-level app.

    Two signals, applied in order:

    1. **Process ancestry** — a listener whose process is a descendant of
       another listener nests under that nearest ancestor. ``ppid_lookup``
       maps a PID to its parent PID (or ``None`` at the top); it is a
       parameter so the walk is unit-testable without real processes. The
       ``seen`` guard makes a pathological cycle terminate instead of
       spinning.
    2. **Shared working directory** (fallback) — a helper a parent app
       spawned **detached** re-parents out of the parent's process tree, so
       ancestry can't see the link (e.g. local-llm-hub spawns its Orpheus
       TTS server and whisper-translate proxy detached, leaving three flat
       "Local Llm Hub" rows). For listeners *still* top-level after step 1,
       group by normalized ``cwd`` — the same app-identity signal
       :func:`misc._app_label_for_dir` already uses — and nest the
       higher-port members under the lowest-port one in each same-dir group.

    Sibling apps in separate trees and separate directories never group.
    """
    listener_pids = {o.pid for o in owners}
    for owner in owners:
        seen: set[int] = {owner.pid}
        cur = ppid_lookup(owner.pid)
        while cur is not None and cur not in seen:
            seen.add(cur)
            if cur in listener_pids:
                owner.parent_pid = cur
                break
            cur = ppid_lookup(cur)

    _group_residual_by_cwd(owners)


def _group_residual_by_cwd(owners: List[PortOwner]) -> None:
    """Nest detached helpers under their app by shared working directory.

    Only considers listeners still top-level after the ancestry pass. Within
    each group of 2+ sharing a normalized ``cwd``, the lowest-port listener is
    the parent and the rest get ``parent_pid`` set to it. Listeners with an
    empty/unknown ``cwd`` never group — that keeps unrelated processes whose
    cwd we couldn't read from collapsing into one bogus app.
    """
    groups: dict[str, List[PortOwner]] = {}
    for owner in owners:
        if owner.parent_pid is not None or not owner.cwd:
            continue
        key = os.path.normcase(os.path.normpath(owner.cwd))
        groups.setdefault(key, []).append(owner)

    for members in groups.values():
        if len(members) < 2:
            continue
        parent = min(members, key=lambda o: o.port)
        for owner in members:
            if owner is not parent:
                owner.parent_pid = parent.pid


def kill_process_tree(pid: int, grace_seconds: float = 3.0) -> List[int]:
    """Kill ``pid`` and every descendant; return the PIDs that were signalled.

    Sends ``terminate()`` to the whole tree, waits up to ``grace_seconds``
    for a clean exit, then ``kill()``s whatever is still alive.  All
    ``psutil`` exceptions are swallowed so callers can finalise their own
    state regardless of how messy the process tree turned out to be.
    A missing root process is treated as already-gone (returns ``[]``).
    """
    if psutil is None:
        return []
    try:
        root = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return []

    procs: List = [root]
    try:
        procs.extend(root.children(recursive=True))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    signalled: List[int] = []
    for proc in procs:
        try:
            proc.terminate()
            signalled.append(proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    try:
        _, alive = psutil.wait_procs(procs, timeout=grace_seconds)
    except psutil.Error:
        alive = procs
    for proc in alive:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    return signalled


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
