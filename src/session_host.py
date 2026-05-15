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
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO, Tuple

import psutil

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

# OSC window-title sequences `claude` emits: ESC ] 0 ; <title> (BEL | ESC \).
# OSC 0 sets icon + title, OSC 2 sets the title — we treat both as the title.
_OSC_TITLE_RE = re.compile(r"\x1b\][02];([^\x07\x1b]*)(?:\x07|\x1b\\)")
# Longest trailing fragment of `_ring` we keep as a carry buffer so a title
# sequence split across two PTY reads still parses.
_TITLE_CARRY_MAX = 512

# Session kinds. "pty" is a launcher-owned ConPTY streamed to the phone;
# "remote" is a detached console window the launcher only tracks (no PTY,
# no scrollback, no WebSocket — the Claude cloud app drives it).
KIND_PTY = "pty"
KIND_REMOTE = "remote"


def _direct_children(pid: int) -> List["psutil.Process"]:
    """Return the immediate child processes of ``pid`` (one level only)."""
    try:
        parent = psutil.Process(pid)
        return parent.children(recursive=False)
    except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
        logger.debug(f"_direct_children({pid}) failed: {exc}")
        return []


def _kill_process_tree(pid: int, label: str = "") -> None:
    """Force-kill ``pid`` and every descendant. Best-effort — never raises."""
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug(f"taskkill {label or pid} failed: {exc}")


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
    title: str = ""
    # PID of the PC-side mirror window (the Edge/Chrome --app browser
    # process opened by ``open_local_terminal_window``). Stashed via
    # ``attach_mirror`` so ``Stop & Close`` can dismiss the window.
    mirror_pid: Optional[int] = None
    _ring: str = ""
    _title_carry: str = ""
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
            self._scan_title(chunk)
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

    def _scan_title(self, chunk: str) -> None:
        """Track the OSC window title `claude` emits, surfaced via ``to_api``.

        Runs on the reader thread. ``_title_carry`` holds the trailing
        fragment of the previous chunk so a title sequence straddling a
        read boundary still parses; only the carry's tail is kept, so this
        stays O(chunk).
        """
        buf = self._title_carry + chunk
        matches = list(_OSC_TITLE_RE.finditer(buf))
        if matches:
            new_title = matches[-1].group(1)
            if new_title != self.title:
                self.title = new_title
        # Keep just enough tail for a sequence split across the boundary.
        self._title_carry = buf[-_TITLE_CARRY_MAX:]

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
        # pywinpty.PtyProcess.write can return fewer bytes than requested
        # (and sometimes 0) when the ConPTY input pipe is busy. The earlier
        # one-shot call dropped the unwritten remainder, which truncated
        # long pastes from the phone's paste button. Loop until everything
        # is written, with a bounded retry budget so a stuck pipe can't
        # hang the websocket pump indefinitely.
        if self._exited or not data:
            return
        remaining = data
        deadline = time.monotonic() + 5.0
        while remaining:
            try:
                n = self._pty.write(remaining)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"PTY {self.session_id[:8]} write failed: {exc}")
                return
            if n:
                remaining = remaining[n:]
                continue
            if time.monotonic() > deadline:
                logger.warning(
                    f"PTY {self.session_id[:8]} write stalled — dropped "
                    f"{len(remaining)} of {len(data)} chars"
                )
                return
            time.sleep(0.01)

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

    def attach_mirror(self, pid: int) -> None:
        """Stash the PID of the PC-side mirror window for later ``kill_mirror``."""
        self.mirror_pid = int(pid) if pid else None
        logger.info(
            f"🪟 PTY {self.session_id[:8]} mirror_pid attached: {self.mirror_pid}"
        )

    def kill_mirror(self) -> bool:
        """Force-close the PC-side mirror browser window. Best-effort.

        Returns True when a mirror PID was known and a kill was attempted.
        """
        pid = self.mirror_pid
        if not pid:
            logger.info(
                f"🪟 PTY {self.session_id[:8]} kill_mirror: no mirror_pid "
                "stashed — nothing to close"
            )
            return False
        logger.info(
            f"🪟 PTY {self.session_id[:8]} kill_mirror: taskkilling pid {pid}"
        )
        _kill_process_tree(pid, label=f"mirror for {self.session_id[:8]}")
        self.mirror_pid = None
        return True

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
            "title": self.title,
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
        _kill_process_tree(
            self._proc.pid, label=f"remote {self.session_id[:8]}"
        )

    def stop_inner_only(self) -> None:
        """Kill just the ``claude.exe`` child, leave the ``cmd.exe`` shell alive.

        Pairs with the ``cmd /k claude`` spawn — when claude is killed the
        outer shell stays open at a fresh prompt, so the user can read the
        final transcript on the PC and close the window manually.
        Falls back to a full tree kill if the child can't be located.
        """
        if self._proc.poll() is not None:
            return
        children = _direct_children(self._proc.pid)
        if not children:
            logger.debug(
                f"remote {self.session_id[:8]} no children to inner-kill — "
                "falling back to full tree kill"
            )
            self.stop()
            return
        for child in children:
            try:
                child.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
                logger.debug(
                    f"remote {self.session_id[:8]} child {child.pid} "
                    f"kill failed: {exc}"
                )

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
        # `cmd /k` (not /c) so the shell survives claude exiting — that way
        # the user can pick "Stop" (kill claude only) and the window stays
        # open with the final transcript visible. CREATE_NEW_CONSOLE gives
        # the window its own console so it stays visible on the PC and
        # outlives this host process. We keep the handle to list / kill it.
        command = f"cmd /k claude {flags}".strip()
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

    def stop(
        self,
        session_id: str,
        mode: str = STOP_QUIT,
        close_window: bool = False,
    ) -> bool:
        """Stop a session. ``close_window`` also dismisses the PC-side window.

        For PTY sessions: stops the PTY as today, then (if ``close_window``)
        force-closes the mirror browser window via :meth:`PtySession.kill_mirror`.
        For remote sessions: ``close_window=True`` does the existing taskkill
        of the whole console tree; ``close_window=False`` kills only the
        inner ``claude.exe`` so the cmd shell — and the window — stay open.
        """
        session = self.get(session_id)
        if session is None:
            return False
        if isinstance(session, RemoteSession):
            if close_window:
                session.stop(mode)
            else:
                session.stop_inner_only()
            return True
        session.stop(mode)
        if close_window and isinstance(session, PtySession):
            session.kill_mirror()
        return True

    def attach_mirror(self, session_id: str, pid: int) -> bool:
        """Stash a mirror-window PID on a PTY session. Returns False if unknown."""
        session = self.get(session_id)
        if session is None or not isinstance(session, PtySession):
            return False
        session.attach_mirror(pid)
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
