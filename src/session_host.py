"""Launcher-owned PTY sessions — the foundation for the phone terminal.

A :class:`PtySession` wraps a ``winpty.PtyProcess`` running the selected
coding agent (see :mod:`src.agents`) inside a ConPTY the launcher owns.
A background reader thread pumps the session's terminal output into a
bounded ring buffer (so a reconnecting client gets scrollback) and to
every live subscriber queue. :class:`SessionManager` owns the set of
live sessions.

This module has no web-framework imports — ``app/session_host/server.py``
is the HTTP + WebSocket surface layered on top of it. It is Windows-only
(ConPTY); the ``winpty`` import is guarded so the module still imports for
``py_compile`` on other platforms.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO, Tuple

from src.agents import DEFAULT_AGENT, command_for, quit_command_for
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

# Chunk-and-pace thresholds for writes into the ConPTY input pipe (#64).
# A real-PTY readback harness (test_session_host_pty_realpty.py) showed the
# write boundary itself delivers multi-KB payloads losslessly — pywinpty's
# write into the input pipe is effectively blocking and does NOT drop the
# tail. So chunking is not a truncation fix (the original #64 framing was
# wrong); it is pacing, which keeps a multi-KB burst from overrunning the
# Windows console input queue that a busy TUI drains slowly. The agent-side
# atomic-paste fix (bracketed-paste framing) lives client-side in
# terminal.js (framePaste). We keep small writes one-shot and split larger
# payloads into ~512 B chunks with a small pause; pywinpty's return value is
# never interpreted as a bytes-accepted count — doing so amplified a single
# keystroke into thousands (#13 revert).
_WRITE_CHUNK_THRESHOLD = 512
_WRITE_CHUNK_SIZE = 512
_WRITE_CHUNK_PAUSE = 0.003

# Stop modes accepted by SessionManager.stop / PtySession.stop.
STOP_INTERRUPT = "interrupt"  # Ctrl+C into the PTY
STOP_QUIT = "quit"            # type "/quit" — Claude Code's clean exit
STOP_KILL = "kill"            # force-terminate the ConPTY

# Graceful-stop grace window: how long STOP_QUIT waits for the agent to exit
# on its own quit command before force-terminating as a fallback (issue #253).
# A clean /quit exits in ~0.7 s empirically; 5 s is generous headroom and
# stays under the session-client stop timeout.
_STOP_GRACE_SECONDS = 5.0

# Session kinds. "pty" is a launcher-owned ConPTY streamed to the phone;
# "remote" is a detached console window the launcher only tracks (no PTY,
# no scrollback, no WebSocket — the Claude cloud app drives it).
KIND_PTY = "pty"
KIND_REMOTE = "remote"

# Spawn a short-lived child without flashing a console window.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# Absolute Windows PowerShell 5.1 — never the bare `pwsh` execution-alias stub
# (a 0-byte reparse point that fails when spawned non-interactively).
_POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


def _ps_quote(value: str) -> str:
    """Escape ``value`` for embedding inside a PowerShell single-quoted string."""
    return value.replace("'", "''")


def _parse_started_pid(stdout: Optional[str]) -> Optional[int]:
    """Pull the PID ``Start-Process -PassThru`` printed (last numeric line)."""
    for line in reversed((stdout or "").splitlines()):
        text = line.strip()
        if text.isdigit():
            return int(text)
    return None


def _pid_alive(pid: int) -> bool:
    """True if ``pid`` names a live process (Windows, via ctypes).

    Detached consoles are orphaned out of the host's process tree (issue
    #130) so we no longer hold a ``Popen`` handle for them — liveness is a
    bare PID probe. ``ctypes.windll`` is touched only at call time, keeping
    the module importable on non-Windows for ``py_compile``.
    """
    if not pid:
        return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle(handle)


def _parse_osc_title(buffer: str) -> Tuple[str, str]:
    """Extract OSC window-title sequences from a buffer.

    Returns (remaining_buffer, extracted_title_or_empty).
    Handles OSC 0 and OSC 2 sequences in both BEL and ST terminator forms.
    Strips ANSI/control chars and caps length.
    """
    extracted = ""
    remaining = buffer

    while True:
        # Look for OSC start: ESC ]
        start_idx = remaining.find("\x1b]")
        if start_idx == -1:
            break

        # Look for terminators: BEL (0x07) or ST (ESC \)
        bel_idx = remaining.find("\x07", start_idx)
        st_idx = remaining.find("\x1b\\", start_idx)

        # Determine which terminator comes first
        end_idx = -1
        term_len = 0
        if bel_idx != -1 and (st_idx == -1 or bel_idx < st_idx):
            end_idx = bel_idx
            term_len = 1
        elif st_idx != -1:
            end_idx = st_idx
            term_len = 2

        if end_idx == -1:
            # Incomplete sequence; keep in buffer for next chunk
            break

        try:
            # Extract the full sequence
            seq = remaining[start_idx : end_idx + term_len]
            # Parse: ESC ] <code> ; <text> <term>
            # Find the code (0 or 2)
            code_end = remaining.find(";", start_idx)
            if code_end != -1 and code_end < end_idx:
                code_part = remaining[start_idx + 2 : code_end].strip()
                if code_part in ("0", "2"):
                    text = remaining[code_end + 1 : end_idx]
                    # Strip ANSI/control chars
                    clean = "".join(c for c in text if ord(c) >= 32 or c in "\t")
                    clean = clean.strip()
                    if clean:
                        # Cap at 80 chars
                        extracted = clean[:80]
        except Exception:
            pass

        # Remove the processed sequence and continue
        remaining = remaining[: start_idx] + remaining[end_idx + term_len :]

    return remaining, extracted


# OSC colour query/reply leak (#270). Codex emits OSC 10/11/12 *queries*
# (``ESC]10;?``) at startup; something answers them and the reply
# (``ESC]10;rgb:…``) leaks as visible text on a fresh/dirty xterm. We strip
# both the query and reply forms of OSC 10/11/12 from the output stream at the
# read boundary so the leak is killed for fresh + reconnect, pc + phone. We
# match TIGHTLY — only OSC 10/11/12 with a ``?`` query or an ``rgb:``/``#``
# colour payload — so we never touch title OSC (0/1/2), hyperlink OSC 8,
# clipboard OSC 52, or any CSI.
#
# Terminator handling differs by form (empirical, from a live pywinpty
# capture of Codex startup): the ``rgb:``/``#`` *reply* always carries a BEL
# (\x07) or ST (ESC \) terminator. The ``?`` *query*, however, is emitted by
# Codex **unterminated and back-to-back** — ``ESC]10;?ESC]11;?`` — each query
# implicitly ended by the next ESC, with no BEL/ST at all. The original #270
# pattern required a terminator on every form, so it silently MISSED these
# bare queries: they reached xterm.js, which answered with ``ESC]10;rgb:…``,
# and that reply leaked. So the terminator is OPTIONAL for the ``?`` query
# (a bare ``ESC]1X;?`` is already a complete colour query) and stays REQUIRED
# for the rgb/# reply (its payload needs a clear end so we don't over-strip).
_OSC_COLOR_RE = re.compile(
    r"\x1b\]1[012];"                                   # OSC 10 | 11 | 12 ;
    r"(?:"
    r"\?(?:\x07|\x1b\\)?"                              # query: terminator OPTIONAL
    r"|(?:rgb:[0-9A-Fa-f/]+|#[0-9A-Fa-f]+)(?:\x07|\x1b\\)"  # reply: term REQUIRED
    r")"
)
# A trailing fragment we must hold back to the next read: either the *start*
# of a colour OSC opened but not yet terminated, OR a complete-but-bare ``?``
# query whose BEL/ST terminator may still land in the next chunk (so we don't
# strip the query here and orphan its terminator into the next chunk). Anchored
# to the END, and tight — it only matches valid colour-OSC prefixes (``?`` /
# ``rgb:…`` / ``#…``), never arbitrary trailing text, because it is consulted
# BEFORE the strip: a loose ``[^\x07\x1b]*`` here would wrongly hold real text
# that merely follows a query in the same chunk.
_OSC_COLOR_PARTIAL_RE = re.compile(
    r"\x1b(?:\](?:1(?:[012](?:;(?:"
    r"\?\x1b?"                                  # query (+ pending ST ESC)
    r"|r(?:g(?:b(?::[0-9A-Fa-f/]*\x1b?)?)?)?"   # rgb:… reply in progress
    r"|#[0-9A-Fa-f]*\x1b?"                      # #… reply in progress
    r")?)?)?)?)?\Z"
)
# Cap on the carried partial: if an unterminated ESC] grows past this without a
# terminator it's not really a colour query — flush it as-is so a stray ESC can
# never wedge the stream.
_OSC_CARRY_MAX = 64


def _strip_color_osc(chunk: str, carry: str) -> Tuple[str, str]:
    """Strip OSC 10/11/12 colour query/reply sequences from ``chunk``.

    Stateful across reads: ``carry`` is any trailing partial colour-OSC
    fragment held back from the previous chunk. Returns
    ``(clean_output, new_carry)`` — ``clean_output`` is safe to emit now,
    ``new_carry`` is the partial fragment to prepend to the next chunk.

    Fast path: a chunk with no ESC at all and an empty carry can't contain a
    colour OSC (nor the start of one straddling the boundary), so it passes
    through untouched.
    """
    if not carry and "\x1b" not in chunk:
        return chunk, ""

    data = carry + chunk

    # Hold back a trailing partial/bare colour-OSC BEFORE stripping, so a
    # sequence split across the read boundary — or a bare ``?`` query whose
    # terminator lands in the next chunk — is caught whole next read instead
    # of being half-stripped (which would orphan a BEL/ST into the next
    # chunk). Bound it: a fragment past the cap is flushed rather than carried
    # forever, so a stray ESC can't wedge the stream.
    m = _OSC_COLOR_PARTIAL_RE.search(data)
    if m and (len(data) - m.start()) <= _OSC_CARRY_MAX:
        head, new_carry = data[: m.start()], data[m.start() :]
    else:
        head, new_carry = data, ""
    return _OSC_COLOR_RE.sub("", head), new_carry


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
    agent: str = DEFAULT_AGENT
    rows: int = 40
    cols: int = 120
    _ring: str = ""
    _ring_lock: threading.Lock = field(default_factory=threading.Lock)
    _subscribers: "set[asyncio.Queue]" = field(default_factory=set)
    _reader: Optional[threading.Thread] = None
    _exited: bool = False
    _transcript: Optional[TextIO] = None
    live_title: str = ""
    _osc_buffer: str = ""
    _color_osc_carry: str = ""

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
            # Strip OSC 10/11/12 colour query/reply sequences (#270) at the
            # source — before scrollback + broadcast — so the leak never
            # reaches a fresh OR reconnecting client. Stateful: a sequence
            # split across two reads is held in _color_osc_carry.
            chunk, self._color_osc_carry = _strip_color_osc(
                chunk, self._color_osc_carry
            )
            if not chunk:
                continue
            # Parse OSC window-title sequences and cache the latest title.
            self._osc_buffer += chunk
            self._osc_buffer, title = _parse_osc_title(self._osc_buffer)
            if title:
                self.live_title = title
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
        if self._exited or not data:
            return
        try:
            if len(data) <= _WRITE_CHUNK_THRESHOLD:
                self._pty.write(data)
                return
            # Long write (paste / large input): pace it in ~512 B chunks so
            # the burst doesn't overrun the console input queue a busy TUI
            # drains slowly (#64). pywinpty's return value is deliberately
            # ignored — interpreting it as a bytes-accepted count amplified a
            # single keystroke into thousands (#13 revert).
            logger.debug(
                f"PTY {self.session_id[:8]} chunked write "
                f"({len(data)} chars / "
                f"{(len(data) + _WRITE_CHUNK_SIZE - 1) // _WRITE_CHUNK_SIZE} chunks)"
            )
            first = True
            for i in range(0, len(data), _WRITE_CHUNK_SIZE):
                if not first:
                    time.sleep(_WRITE_CHUNK_PAUSE)
                self._pty.write(data[i : i + _WRITE_CHUNK_SIZE])
                first = False
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

    def stop(
        self, mode: str = STOP_QUIT, grace_seconds: float = _STOP_GRACE_SECONDS
    ) -> None:
        """Stop the session: graceful agent-own quit, then force-fallback.

        ``STOP_QUIT`` (the path the single "Stop and kill" button drives)
        types the agent's *own* quit command — Claude's ``/quit``,
        Copilot's ``/exit``, … (see :func:`quit_command_for`) — after an
        ESC that clears any partial prompt, then waits up to
        ``grace_seconds`` for the agent to exit on its own. The clean exit
        lets the agent run its shutdown path (Claude Code SessionEnd hooks,
        transcript finalisation, …) deterministically, rather than relying
        on the abnormal console-close path a bare force-terminate triggers.
        Only if the agent has not exited within the grace window do we
        force-terminate the ConPTY — the guarantee that a stop always ends
        the session (issue #253).

        ``STOP_KILL`` force-terminates immediately (no graceful step);
        ``STOP_INTERRUPT`` sends Ctrl+C and leaves the session running.
        Every *terminating* stop signals subscribers so the mirror page
        self-closes; an interrupt does not.
        """
        if mode == STOP_INTERRUPT:
            try:
                self._pty.sendintr()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"PTY {self.session_id[:8]} interrupt failed: {exc}")
            return  # not a termination — leave the session (and mirror) alive

        try:
            if mode == STOP_KILL:
                self._pty.terminate(force=True)
            else:  # STOP_QUIT — graceful, agent-appropriate, with fallback.
                # ESC clears any partial input so the quit command lands on
                # an empty prompt.
                self._pty.write("\x1b")
                self._pty.write(quit_command_for(self.agent) + "\r")
                deadline = time.monotonic() + max(0.0, grace_seconds)
                while time.monotonic() < deadline:
                    if not self.alive:
                        break
                    time.sleep(0.1)
                if self.alive:
                    logger.info(
                        f"PTY {self.session_id[:8]} did not exit on "
                        f"{quit_command_for(self.agent)!r} within "
                        f"{grace_seconds:.0f}s — force-terminating"
                    )
                    self._pty.terminate(force=True)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"PTY {self.session_id[:8]} stop({mode}) failed: {exc}")

        # Every terminating stop closes the window — signal mirror page(s).
        self._signal_shutdown_to_subscribers()

    def force_kill(self) -> None:
        try:
            self._pty.terminate(force=True)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"PTY {self.session_id[:8]} force-kill failed: {exc}")

    def _signal_shutdown_to_subscribers(self) -> None:
        """Send a shutdown message to all WebSocket subscribers (mirror page)."""
        import json
        msg = json.dumps({"type": "shutdown"})
        with self._ring_lock:
            subscribers = list(self._subscribers)
        for queue in subscribers:
            try:
                self._loop.call_soon_threadsafe(queue.put_nowait, msg)
            except Exception:  # noqa: BLE001
                pass

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
            "agent": self.agent,
            "project_dir": self.project_dir,
            "name": self.name,
            "flags": self.flags,
            "started_at": self.started_at,
            "alive": self.alive,
            "rows": self.rows,
            "cols": self.cols,
            "live_title": self.live_title,
        }


class RemoteSession:
    """A detached ``claude`` window the launcher tracks but does not stream.

    Spawned in its own console window and deliberately **orphaned out of the
    session-host's process tree** (see :meth:`SessionManager.create_remote`),
    so it stays visible on the PC and survives a session-host *or* a
    ``tray.bat --restart`` tear-down (issue #130) — that survival is the whole
    point of the detached mode. The launcher keeps only the console PID, so the
    session shows up in the running-sessions list and can be killed from the
    phone — there is no PTY, no scrollback, and no WebSocket. Remote control
    comes from the Claude cloud app.
    """

    kind = KIND_REMOTE

    def __init__(
        self,
        session_id: str,
        project_dir: str,
        name: str,
        flags: str,
        started_at: float,
        pid: int,
        agent: str = DEFAULT_AGENT,
    ) -> None:
        self.session_id = session_id
        self.project_dir = project_dir
        self.name = name
        self.flags = flags
        self.started_at = started_at
        self._pid = pid
        self.agent = agent

    @property
    def alive(self) -> bool:
        return _pid_alive(self._pid)

    def stop(
        self, mode: str = STOP_KILL, grace_seconds: float = _STOP_GRACE_SECONDS
    ) -> None:
        """Stop and close the detached console session.

        Detached processes (RemoteSession) cannot be gracefully stopped without
        closing the window, since they have no stdin/PTY. We use taskkill /T /F
        to terminate the console's whole subtree (cmd.exe + agent + children).
        The console is orphaned from the host tree, but it is still reachable by
        its own PID, so an explicit Stop from the phone still works.

        ``mode`` / ``grace_seconds`` are accepted for interface parity with
        :class:`PtySession` but ignored — there is no PTY to type a quit into.
        """
        if not _pid_alive(self._pid):
            return

        try:
            subprocess.run(
                ["taskkill", "/PID", str(self._pid), "/T", "/F"],
                capture_output=True,
                creationflags=_CREATE_NO_WINDOW,
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
            "agent": self.agent,
            "project_dir": self.project_dir,
            "name": self.name,
            "flags": self.flags,
            "started_at": self.started_at,
            "alive": self.alive,
            "live_title": "",
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

    def create(
        self,
        project_dir: str,
        name: str,
        flags: str,
        agent: str = DEFAULT_AGENT,
        rows: int = 40,
        cols: int = 120,
    ) -> PtySession:
        """Spawn ``<agent> <flags>`` inside a fresh ConPTY in ``project_dir``.

        ``agent`` selects which coding CLI to run; see :mod:`src.agents`.

        ``rows``/``cols`` size the ConPTY at spawn time. The phone passes
        its real terminal dimensions through the launch request so a
        full-screen differential TUI (Codex's ratatui) paints its *first*
        frame at the correct width instead of the legacy ``40×120`` — which
        wrapped/cut on a portrait phone (issue #126). They are clamped to
        the same bounds as :meth:`PtySession.resize`; an omitted value
        falls back to the legacy default.
        """
        if PtyProcess is None:
            raise RuntimeError("pywinpty is not available — cannot spawn a PTY")
        if self._loop is None:
            raise RuntimeError("SessionManager has no event loop attached")
        directory = Path(project_dir)
        if not directory.is_dir():
            raise OSError(f"Project directory not found: {project_dir}")

        rows = max(1, min(int(rows), 1000))
        cols = max(1, min(int(cols), 1000))
        session_id = uuid.uuid4().hex
        # `cmd /c` resolves the agent command (e.g. claude.cmd / agy.cmd)
        # off PATH the way a normal shell would; when the agent exits, cmd
        # exits, the PTY closes, and the reader thread sees EOF.
        exe = command_for(agent)
        command = f"cmd /c {exe} {flags}".strip()
        pty = PtyProcess.spawn(
            command, cwd=str(directory), dimensions=(rows, cols)
        )
        session = PtySession(
            session_id=session_id,
            project_dir=str(directory),
            name=name,
            flags=flags,
            started_at=time.time(),
            _loop=self._loop,
            _pty=pty,
            agent=agent,
            rows=rows,
            cols=cols,
        )
        session.start_reader()
        with self._lock:
            self._sessions[session_id] = session
        logger.info(
            f"🚀 PTY session {session_id[:8]} spawned: {exe} in {directory} "
            f"({flags})"
        )
        return session

    def create_remote(
        self, project_dir: str, name: str, flags: str, agent: str = DEFAULT_AGENT
    ) -> RemoteSession:
        """Spawn ``<agent> <flags>`` in a detached console window.

        The window is **orphaned out of the session-host's process tree** so a
        ``tray.bat --restart`` — which tears the tray subtree down with
        ``taskkill /T`` — cannot cascade into it (issue #130); detached
        sessions are meant to outlive a launcher / session-host restart, which
        is the entire point of the mode. We launch through a transient
        PowerShell ``Start-Process`` that exits the moment the console is up:
        the console's parent is that PowerShell, so once it exits the console
        is reparented away from the host subtree and ``taskkill /T`` on the
        tray can no longer enumerate it. ``-PassThru`` hands back the real
        console PID, which we keep purely to list/kill it.

        Tracked for listing and kill only — see :class:`RemoteSession`.
        """
        directory = Path(project_dir)
        if not directory.is_dir():
            raise OSError(f"Project directory not found: {project_dir}")
        session_id = uuid.uuid4().hex
        # `cmd /c` resolves the agent command (e.g. claude.cmd) off PATH and
        # closes the window when the agent exits — same shape as the PTY spawn.
        exe = command_for(agent)
        inner = f"{exe} {flags}".strip()
        ps_command = (
            "(Start-Process -FilePath 'cmd' "
            f"-ArgumentList '/c {_ps_quote(inner)}' "
            f"-WorkingDirectory '{_ps_quote(str(directory))}' -PassThru).Id"
        )
        result = subprocess.run(
            [_POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", ps_command],
            capture_output=True,
            text=True,
            creationflags=_CREATE_NO_WINDOW,
            timeout=30,
        )
        pid = _parse_started_pid(result.stdout)
        if pid is None:
            detail = (result.stderr or result.stdout or "").strip()[:200]
            raise RuntimeError(f"Failed to launch detached session: {detail}")
        session = RemoteSession(
            session_id=session_id,
            project_dir=str(directory),
            name=name,
            flags=flags,
            started_at=time.time(),
            pid=pid,
            agent=agent,
        )
        with self._lock:
            self._sessions[session_id] = session
        logger.info(
            f"🚀 remote session {session_id[:8]} spawned (orphaned, pid={pid}): "
            f"{exe} in {directory} ({flags})"
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
        self, session_id: str, mode: str = STOP_QUIT,
        grace_seconds: float = _STOP_GRACE_SECONDS,
    ) -> bool:
        session = self.get(session_id)
        if session is None:
            return False
        session.stop(mode, grace_seconds=grace_seconds)
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
