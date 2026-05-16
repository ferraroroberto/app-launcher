"""Launcher-owned PTY sessions — the foundation for the phone terminal.

A :class:`PtySession` wraps a ``winpty.PtyProcess`` running ``claude``
inside a ConPTY the launcher owns. A background reader thread pumps the
session's terminal output into a bounded ring buffer (so a reconnecting
client gets scrollback) and to every live subscriber queue.
:class:`SessionManager` owns the set of live sessions.

This module has no web-framework imports — ``app/session_host/server.py``
is the HTTP + WebSocket surface layered on top of it. It is Windows-only
(ConPTY); the ``winpty`` import is guarded so the module still imports for
``py_compile`` on other platforms.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO, Tuple

from src.audit import transcript_path

try:  # Windows-only — ConPTY via pywinpty.
    from winpty import PtyProcess  # type: ignore
except ImportError:  # pragma: no cover — non-Windows / missing dep
    PtyProcess = None  # type: ignore

logger = logging.getLogger(__name__)

# How much terminal output to keep per session for scrollback-on-reconnect.
_RING_MAX_CHARS = 256 * 1024
# Chunk size for the blocking PTY read in the reader thread.
_READ_CHUNK = 4096
# Sentinel pushed to subscribers when the session ends.
_EOF = object()

# Stop modes accepted by SessionManager.stop / PtySession.stop.
STOP_INTERRUPT = "interrupt"  # Ctrl+C into the PTY
STOP_QUIT = "quit"            # type "/quit" — Claude Code's clean exit
STOP_KILL = "kill"            # force-terminate the ConPTY

# Session kinds. "pty" is a launcher-owned ConPTY streamed to the phone;
# "remote" is a detached console window the launcher only tracks (no PTY,
# no scrollback, no WebSocket — the Claude cloud app drives it).
KIND_PTY = "pty"
KIND_REMOTE = "remote"


@dataclass
class PtySession:
    """One ``claude`` process running inside a launcher-owned ConPTY."""

    kind = KIND_PTY

    session_id: str
    project_dir: str
    name: str
    flags: str
    started_at: float
    _loop: asyncio.AbstractEventLoop
    _pty: "PtyProcess"  # type: ignore[name-defined]
    rows: int = 40
    cols: int = 120
    _ring: str = ""
    _ring_lock: threading.Lock = field(default_factory=threading.Lock)
    _subscribers: "set[asyncio.Queue]" = field(default_factory=set)
    _reader: Optional[threading.Thread] = None
    _exited: bool = False
    _transcript: Optional[TextIO] = None

    # ------------------------------------------------------------ lifecycle
    def start_reader(self) -> None:
        """Spawn the background thread that pumps PTY output to subscribers."""
        try:
            path = transcript_path(self.session_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._transcript = path.open("a", encoding="utf-8", errors="replace")
            self._transcript.write(
                f"\n=== session {self.session_id} :: {self.name} :: "
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} ===\n"
            )
            self._transcript.flush()
        except OSError as exc:  # pragma: no cover
            logger.debug(f"transcript open failed: {exc}")
            self._transcript = None
        self._reader = threading.Thread(
            target=self._read_loop, name=f"pty-{self.session_id[:8]}", daemon=True
        )
        self._reader.start()

    def _read_loop(self) -> None:
        while True:
            try:
                chunk = self._pty.read(_READ_CHUNK)
            except EOFError:
                break
            except Exception as exc:  # noqa: BLE001 — WinptyError et al.
                logger.debug(f"PTY {self.session_id[:8]} read ended: {exc}")
                break
            if not chunk:
                # pywinpty returns "" only transiently; a dead PTY raises.
                if not self._pty.isalive():
                    break
                time.sleep(0.01)
                continue
            with self._ring_lock:
                self._ring += chunk
                if len(self._ring) > _RING_MAX_CHARS:
                    self._ring = self._ring[-_RING_MAX_CHARS:]
                subscribers = list(self._subscribers)
            if self._transcript is not None:
                try:
                    self._transcript.write(chunk)
                    self._transcript.flush()
                except OSError:  # pragma: no cover
                    pass
            for queue in subscribers:
                self._loop.call_soon_threadsafe(queue.put_nowait, chunk)
        self._exited = True
        with self._ring_lock:
            subscribers = list(self._subscribers)
        for queue in subscribers:
            self._loop.call_soon_threadsafe(queue.put_nowait, _EOF)
        if self._transcript is not None:
            try:
                self._transcript.write(
                    f"\n=== ended {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n"
                )
                self._transcript.close()
            except OSError:  # pragma: no cover
                pass
            self._transcript = None
        logger.info(f"⏹️  PTY session {self.session_id[:8]} ({self.name}) ended")

    # ----------------------------------------------------------- subscribe
    def subscribe(self) -> Tuple[str, "asyncio.Queue"]:
        """Register a subscriber. Returns the scrollback snapshot + its queue.

        The snapshot and the registration happen under one lock so no
        output chunk is lost or double-delivered across the handover.
        """
        queue: asyncio.Queue = asyncio.Queue()
        with self._ring_lock:
            snapshot = self._ring
            self._subscribers.add(queue)
        if self._exited:
            queue.put_nowait(_EOF)
        return snapshot, queue

    def unsubscribe(self, queue: "asyncio.Queue") -> None:
        with self._ring_lock:
            self._subscribers.discard(queue)

    # --------------------------------------------------------------- io
    def write(self, data: str) -> None:
        if self._exited:
            return
        try:
            self._pty.write(data)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"PTY {self.session_id[:8]} write failed: {exc}")

    def resize(self, rows: int, cols: int) -> None:
        rows = max(1, min(rows, 1000))
        cols = max(1, min(cols, 1000))
        self.rows = rows
        self.cols = cols
        try:
            self._pty.setwinsize(rows, cols)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"PTY {self.session_id[:8]} resize failed: {exc}")

    def stop(self, mode: str = STOP_QUIT) -> None:
        """Stop the session — clean ``/quit`` by default, or interrupt / kill."""
        try:
            if mode == STOP_INTERRUPT:
                self._pty.sendintr()
            elif mode == STOP_KILL:
                self._pty.terminate(force=True)
            else:  # STOP_QUIT
                # Claude Code's clean exit. ESC clears any partial input
                # first so "/quit" lands on an empty prompt.
                self._pty.write("\x1b")
                self._pty.write("/quit\r")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"PTY {self.session_id[:8]} stop({mode}) failed: {exc}")

    def force_kill(self) -> None:
        try:
            self._pty.terminate(force=True)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"PTY {self.session_id[:8]} force-kill failed: {exc}")

    @property
    def alive(self) -> bool:
        if self._exited:
            return False
        try:
            return bool(self._pty.isalive())
        except Exception:  # noqa: BLE001
            return False

    def to_api(self) -> Dict[str, object]:
        return {
            "session_id": self.session_id,
            "kind": self.kind,
            "project_dir": self.project_dir,
            "name": self.name,
            "flags": self.flags,
            "started_at": self.started_at,
            "alive": self.alive,
            "rows": self.rows,
            "cols": self.cols,
        }


class RemoteSession:
    """A detached ``claude`` window the launcher tracks but does not stream.

    Spawned in its own console (``CREATE_NEW_CONSOLE``) so it has a visible
    window on the PC and survives a session-host restart. The launcher keeps
    the process handle only so the session shows up in the running-sessions
    list and can be killed from the phone — there is no PTY, no scrollback,
    and no WebSocket. Remote control comes from the Claude cloud app.
    """

    kind = KIND_REMOTE

    def __init__(
        self,
        session_id: str,
        project_dir: str,
        name: str,
        flags: str,
        started_at: float,
        proc: "subprocess.Popen",
    ) -> None:
        self.session_id = session_id
        self.project_dir = project_dir
        self.name = name
        self.flags = flags
        self.started_at = started_at
        self._proc = proc

    @property
    def alive(self) -> bool:
        return self._proc.poll() is None

    def stop(self, mode: str = STOP_KILL) -> None:
        """Kill the detached console and its child process tree.

        ``mode`` is accepted for interface parity with :class:`PtySession`
        but ignored — a remote window has no PTY to send ``/quit`` into.
        """
        if self._proc.poll() is not None:
            return
        try:
            subprocess.run(
                ["taskkill", "/PID", str(self._proc.pid), "/T", "/F"],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug(f"remote {self.session_id[:8]} taskkill failed: {exc}")

    def force_kill(self) -> None:
        self.stop()

    def to_api(self) -> Dict[str, object]:
        return {
            "session_id": self.session_id,
            "kind": self.kind,
            "project_dir": self.project_dir,
            "name": self.name,
            "flags": self.flags,
            "started_at": self.started_at,
            "alive": self.alive,
        }


class SessionManager:
    """Owns every launcher-spawned PTY session for the life of the host."""

    def __init__(self) -> None:
        self._sessions: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Capture the event loop the HTTP surface runs on (called at startup)."""
        self._loop = loop

    def create(self, project_dir: str, name: str, flags: str) -> PtySession:
        """Spawn ``claude <flags>`` inside a fresh ConPTY in ``project_dir``."""
        if PtyProcess is None:
            raise RuntimeError("pywinpty is not available — cannot spawn a PTY")
        if self._loop is None:
            raise RuntimeError("SessionManager has no event loop attached")
        directory = Path(project_dir)
        if not directory.is_dir():
            raise OSError(f"Project directory not found: {project_dir}")

        session_id = uuid.uuid4().hex
        # `cmd /c` resolves `claude` (claude.cmd / node shim) off PATH the
        # way a normal shell would; when claude exits, cmd exits, the PTY
        # closes, and the reader thread sees EOF.
        command = f"cmd /c claude {flags}".strip()
        pty = PtyProcess.spawn(
            command, cwd=str(directory), dimensions=(40, 120)
        )
        session = PtySession(
            session_id=session_id,
            project_dir=str(directory),
            name=name,
            flags=flags,
            started_at=time.time(),
            _loop=self._loop,
            _pty=pty,
        )
        session.start_reader()
        with self._lock:
            self._sessions[session_id] = session
        logger.info(
            f"🚀 PTY session {session_id[:8]} spawned: claude in {directory} "
            f"({flags})"
        )
        return session

    def create_remote(
        self, project_dir: str, name: str, flags: str
    ) -> RemoteSession:
        """Spawn ``claude <flags>`` in a detached console window.

        Tracked for listing and kill only — see :class:`RemoteSession`.
        """
        directory = Path(project_dir)
        if not directory.is_dir():
            raise OSError(f"Project directory not found: {project_dir}")
        session_id = uuid.uuid4().hex
        # `cmd /c` resolves `claude` off PATH; CREATE_NEW_CONSOLE gives the
        # window its own console so it stays visible on the PC and outlives
        # this host process. We keep the handle purely to list / kill it.
        command = f"cmd /c claude {flags}".strip()
        proc = subprocess.Popen(
            command,
            cwd=str(directory),
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            close_fds=True,
        )
        session = RemoteSession(
            session_id=session_id,
            project_dir=str(directory),
            name=name,
            flags=flags,
            started_at=time.time(),
            proc=proc,
        )
        with self._lock:
            self._sessions[session_id] = session
        logger.info(
            f"🚀 remote session {session_id[:8]} spawned: claude in "
            f"{directory} ({flags})"
        )
        return session

    def get(self, session_id: str) -> Optional[Any]:
        with self._lock:
            return self._sessions.get(session_id)

    def list(self) -> List[Any]:
        with self._lock:
            sessions = list(self._sessions.values())
        sessions.sort(key=lambda s: s.started_at)
        return sessions

    def stop(self, session_id: str, mode: str = STOP_QUIT) -> bool:
        session = self.get(session_id)
        if session is None:
            return False
        session.stop(mode)
        return True

    def remove(self, session_id: str) -> Optional[Any]:
        with self._lock:
            return self._sessions.pop(session_id, None)

    def reap_dead(self) -> int:
        """Drop sessions whose process has exited. Returns the count reaped."""
        with self._lock:
            dead = [sid for sid, s in self._sessions.items() if not s.alive]
            for sid in dead:
                self._sessions.pop(sid, None)
        return len(dead)

    def shutdown(self) -> None:
        """Force-kill PTY sessions on host exit; leave detached ones running.

        Detached (``RemoteSession``) windows are meant to outlive the
        launcher — that's the whole point of the remote mode.
        """
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            if isinstance(session, PtySession):
                session.force_kill()
