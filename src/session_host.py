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
import logging
import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO, Tuple

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover — psutil is in requirements.txt
    psutil = None  # type: ignore

from src.agents import (
    DEFAULT_AGENT,
    command_for,
    is_fullscreen,
    is_safe_command_text,
    quit_command_for,
)
from src.audit import transcript_path
from src.env_path import effective_path
from src.session_host_input import InputProtocol
from src.session_host_scan import (  # noqa: F401 — re-exported for callers/tests
    _PROMPT_TITLE_MAX_CHARS,
    _PROMPT_TITLE_MAX_WORDS,
    _cook_input_line,
    _derive_prompt_title,
    _parse_osc_title,
    _scan_bracketed_paste_mode,
    _strip_color_osc,
)
from src.subprocess_flags import NO_WINDOW
from src.vt_snapshot import VtSnapshot

try:  # Windows-only — ConPTY via pywinpty.
    from winpty import PtyProcess  # type: ignore
except ImportError:  # pragma: no cover — non-Windows / missing dep
    PtyProcess = None  # type: ignore

logger = logging.getLogger(__name__)

# How much terminal output to keep per session for scrollback-on-reconnect.
_RING_MAX_CHARS = 256 * 1024
# After a hard ring truncation, advance the head to the next newline within
# this window so a replay never starts mid-escape-sequence (#444) — a cut
# inside a CSI/OSC renders its tail as literal garbage at the top of the
# replayed scrollback. One rendered line (with its SGR decoration) is far
# shorter than this; if no newline appears in the window, keep the raw cut
# rather than discard more history.
_RING_TRIM_SCAN = 4096
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

# The server-initiated input-delivery protocol — bracketed-paste framing,
# bulk settle-then-submit, ingest/echo verification, and the deferred-submit
# watcher (issues #611/#760/#763) — lives in :mod:`src.session_host_input`
# (issue #798). Only :class:`InputProtocol` is imported here (as
# ``PtySession``'s base); callers/tests needing its constants or
# :class:`InputOutcome` import them from :mod:`src.session_host_input`
# directly.

# First-prompt session title (issue #266): how much un-submitted input we
# buffer while waiting for the first submit — a line the user never sends
# must not grow unbounded. This is session *state* (the buffer lives on
# ``PtySession``), unlike the deterministic derivation itself
# (``_derive_prompt_title``/``_PROMPT_TITLE_MAX_CHARS``/``_PROMPT_TITLE_MAX_WORDS``),
# which is a pure function and lives in :mod:`src.session_host_scan`.
_PROMPT_TITLE_BUF_MAX = 400

# Manual title override (issue #458): a launcher-native rename that wins over
# every auto-derived title (live_title/prompt_title/shared_name — see
# sessions.js::sessionTitle) and, unlike those, also reaches a detached
# RemoteSession — the one rename channel that works identically across all
# launcher-supported agents without depending on agent-native OSC support.
# Kept in-memory on the session object (like live_title/prompt_title) rather
# than persisted: it dies with the session, so it needs no cleanup/pruning.
_MANUAL_TITLE_MAX_CHARS = 60

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

# Absolute Windows PowerShell 5.1 — never the bare `pwsh` execution-alias stub
# (a 0-byte reparse point that fails when spawned non-interactively).
_POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

# The allowlist for the ``flags`` string SessionManager.create/create_remote
# splice straight into ``cmd /c <exe> <flags>`` (create) or a PowerShell
# ``Start-Process -ArgumentList`` (create_remote) — the boundary that
# actually owns the command line (issue #753). A superset of
# agents.native_session_name_flags_for's title allowlist: flags legitimately
# carry a literal `"` (a quoted prompt token, e.g. board.py's
# `"/issue-start 123"`), `=` (a config override like
# `model_reasoning_effort=high`), and `@` (the loopback SSH agent's
# caller-supplied `user@host` target — README "session-host also exposes …
# to trusted sibling services"), none of which a bare title may safely
# contain. See agents.is_safe_command_text for the shared predicate and the
# full metacharacter rationale.
_SAFE_FLAGS_CHARS = ' ".,:;()[]{}_-/#?=@'


def _validate_flags(flags: str) -> None:
    """Raise ``ValueError`` if ``flags`` carries a command-line metacharacter.

    Every current caller already keeps ``flags`` safe at its own call site
    (board.py's title allowlist, life_os.py's UUID/slug validation,
    apps.py/board_spawn.py's server-computed ``build_*_flags`` output) — this
    re-checks the same invariant at the one place that actually assembles
    the command line, so a future caller inherits it by construction instead
    of having to rediscover and re-apply it.
    """
    if not is_safe_command_text(flags, _SAFE_FLAGS_CHARS):
        raise ValueError(f"unsafe characters in flags: {flags!r}")


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
    """True if ``pid`` names a live process.

    Detached consoles are orphaned out of the host's process tree (issue
    #130) so we no longer hold a ``Popen`` handle for them — liveness is a
    bare PID probe, answered by ``psutil`` (already a hard dependency —
    ``src.diagnostics.is_pid_alive`` answers the identical question).
    """
    if not pid:
        return False
    if psutil is None:  # pragma: no cover — psutil is in requirements.txt
        return True  # can't introspect — assume alive, don't kill blindly
    return psutil.pid_exists(pid)


def _trim_ring_head(ring: str) -> str:
    """Advance a freshly hard-truncated ring to the next newline boundary.

    Escape sequences never span a literal ``\\n`` (CSI parameter bytes are
    printable; OSC titles carry no newlines), so resuming right after one
    guarantees the replay head is not mid-sequence. Bounded by
    ``_RING_TRIM_SCAN``: with no newline in the window (one enormous
    unbroken line), the raw cut stands — better an approximate head than
    throwing away more history.
    """
    nl = ring.find("\n", 0, _RING_TRIM_SCAN)
    if nl == -1:
        return ring
    return ring[nl + 1 :]


@dataclass
class PtySession(InputProtocol):
    """One ``claude`` process running inside a launcher-owned ConPTY.

    The server-initiated input-delivery protocol (``submit_input`` and
    everything downstream of it — settle-then-submit, ingest/echo
    verification, the deferred-submit watcher) is supplied by
    :class:`~src.session_host_input.InputProtocol`, not defined here; this
    class keeps only the raw PTY-lifecycle concerns plus ``write``/
    ``_write_locked``, the chunk-and-pace write path the protocol builds on.
    """

    kind = KIND_PTY

    session_id: str
    project_dir: str
    name: str
    flags: str
    started_at: float
    _loop: asyncio.AbstractEventLoop
    _pty: "PtyProcess"  # type: ignore[name-defined]
    agent: str = DEFAULT_AGENT
    # Free-form role tag set at create time (e.g. "chief", #245) so callers
    # can find a purpose-built session deterministically; "" for normal ones.
    label: str = ""
    rows: int = 40
    cols: int = 120
    _ring: str = ""
    _ring_lock: threading.Lock = field(default_factory=threading.Lock)
    # Serializes the *input* side (issue #721). ``_ring_lock`` guards output
    # only, and the manager's own lock guards the sessions registry — neither
    # covers the PTY write path, which has had two concurrent writers since
    # #611 added the HTTP ``/sessions/{sid}/input`` route alongside the
    # WebSocket pump. A PC mirror (``role=pc``) and a phone (``role=phone``)
    # attached to the same session is the intended shape, so two writers on
    # one PTY is normal, not exotic. Held across the whole chunk loop in
    # :meth:`write` and across the whole text→settle→CR sequence in
    # :meth:`submit_input`, so a keystroke can neither interleave at ~512 B
    # chunk granularity inside somebody else's paste nor land between a
    # paste and its submitting CR.
    _write_lock: threading.Lock = field(default_factory=threading.Lock)
    _subscribers: "set[asyncio.Queue]" = field(default_factory=set)
    _reader: Optional[threading.Thread] = None
    _exited: bool = False
    _transcript: Optional[TextIO] = None
    live_title: str = ""
    prompt_title: str = ""
    manual_title: str = ""
    _osc_buffer: str = ""
    _color_osc_carry: str = ""
    _prompt_raw: str = ""
    _prompt_captured: bool = False
    # Headless VT mirror for full-screen (ratatui) agents only (issue #432)
    # — Claude's raw-ring replay path never needs one. None for a
    # non-fullscreen agent, or before SessionManager wires it in.
    _vt: Optional["VtSnapshot"] = None
    # Settle-then-submit state for server-initiated writes (issue #611).
    # _bracketed_paste_mode mirrors xterm's term.modes.bracketedPasteMode,
    # tracked off the PTY's own DECSET 2004 output (default False — matches
    # the client-side gate's own default before any signal has been seen).
    # _last_output_at mirrors terminal-compose.js's t.lastOutputAt, used by
    # the bulk-settle wait to detect the paste's ingest has gone quiet.
    _bracketed_paste_mode: bool = False
    _decset_carry: str = ""
    _last_output_at: float = 0.0
    # Monotonic count of every character ever pumped out of this PTY (issue
    # #760). Unlike to_api()'s output_chars — the ring's length, which
    # saturates at _RING_MAX_CHARS — this keeps counting, so a write can mark
    # a position and later ask "what has the terminal painted since?" even on
    # a long-lived session whose ring has wrapped many times over.
    _output_total: int = 0
    # The most recent server-initiated write's outcome (issue #760), surfaced
    # in to_api() so a session that has gone deaf to API input is visible as
    # such instead of being indistinguishable from a busy agent.
    last_input: Optional[Dict[str, Any]] = None
    # Deferred-submit generation counter (issue #763). Every new
    # server-initiated write — and every stop — bumps it, which supersedes any
    # watcher still waiting to press Enter: its "my payload is the thing
    # sitting unsent in the composer" premise no longer holds once somebody
    # else has written to the PTY. A superseded watcher exits without firing
    # and without touching ``last_input``, which by then belongs to the newer
    # call. Raw keystrokes from the WebSocket pump deliberately do *not* bump
    # it — a PC mirror typing anywhere would otherwise cancel every steer, and
    # the pre-fire re-check already handles a composer the human has changed.
    _defer_seq: int = 0
    # Set by _submit_input_locked when it hands a submit to the watcher, read
    # (and cleared) by submit_input once the ``deferred`` verdict is recorded,
    # so the watcher can never record its own outcome first.
    _defer_args: Optional[Tuple[int, int, List[str]]] = None

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
            self._last_output_at = time.time()
            # Track DECSET 2004 (bracketed-paste mode) off the raw output —
            # BEFORE any stripping below, since this must see exactly what a
            # real terminal client would (issue #611).
            paste_mode, self._decset_carry = _scan_bracketed_paste_mode(
                chunk, self._decset_carry
            )
            if paste_mode is not None:
                self._bracketed_paste_mode = paste_mode
            if self._vt is not None:
                self._vt.feed(chunk)
            # Parse OSC window-title sequences and cache the latest title.
            self._osc_buffer += chunk
            self._osc_buffer, title = _parse_osc_title(self._osc_buffer)
            if title:
                self.live_title = title
            with self._ring_lock:
                self._output_total += len(chunk)
                self._ring += chunk
                if len(self._ring) > _RING_MAX_CHARS:
                    self._ring = _trim_ring_head(self._ring[-_RING_MAX_CHARS:])
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
    def _maybe_capture_prompt(self, data: str) -> None:
        """Accumulate input until the first submit, then derive a title (#266).

        Every keystroke and paste the browser sends routes through
        :meth:`write`, so this is the single chokepoint that sees the user's
        first prompt. We buffer raw input until the first CR/LF (the submit),
        cook it to visible text, and store a derived title — used as the
        display name for agents that don't self-name per conversation. Stops
        after the first capture; bounded so an un-submitted line can't grow
        without limit.
        """
        if self._prompt_captured:
            return
        self._prompt_raw += data
        while True:
            cr = self._prompt_raw.find("\r")
            lf = self._prompt_raw.find("\n")
            ends = [i for i in (cr, lf) if i != -1]
            if not ends:
                # No submit yet. If the user has typed a wall of input without
                # ever sending it, give up and title from what we have rather
                # than buffering without bound.
                if len(self._prompt_raw) > _PROMPT_TITLE_BUF_MAX:
                    self._finalize_prompt_title(self._prompt_raw)
                return
            end = min(ends)
            title = _derive_prompt_title(_cook_input_line(self._prompt_raw[:end]))
            if title:
                self._finalize_prompt_title_with(title)
                return
            # Empty submit (a bare Enter / whitespace-only line) — drop it and
            # keep looking, so the first *meaningful* prompt wins. Skip the
            # terminator, collapsing a paired CR+LF.
            nxt = end + 1
            if (self._prompt_raw[end] == "\r" and nxt < len(self._prompt_raw)
                    and self._prompt_raw[nxt] == "\n"):
                nxt += 1
            self._prompt_raw = self._prompt_raw[nxt:]

    def _finalize_prompt_title(self, raw_line: str) -> None:
        self._finalize_prompt_title_with(
            _derive_prompt_title(_cook_input_line(raw_line))
        )

    def _finalize_prompt_title_with(self, title: str) -> None:
        self._prompt_captured = True
        self._prompt_raw = ""
        if title:
            self.prompt_title = title

    def write(self, data: str) -> bool:
        """Write ``data`` into the PTY. Returns whether it was actually sent.

        ``False`` means the write never reached ``self._pty`` at all — the
        session had already exited, or the underlying write raised — so a
        caller (the session-host's ``/input`` route, issue #607) can turn a
        drop into an honest failure instead of the previous unconditional
        ``{"ok": true}`` regardless of outcome. An empty payload is trivially
        ``True``: there was nothing to deliver, so nothing was dropped.

        Serialized on ``_write_lock`` (issue #721) so a concurrent writer
        can't interleave its own bytes between this payload's chunks.
        """
        with self._write_lock:
            return self._write_locked(data)

    def _write_locked(self, data: str) -> bool:
        """:meth:`write`'s body, assuming ``_write_lock`` is already held.

        Exists so :meth:`submit_input` can hold the lock across its whole
        text→settle→CR sequence while still reusing the chunk-and-pace
        logic, without needing a reentrant lock.
        """
        if not data:
            return True
        if self._exited:
            return False
        if not self._prompt_captured:
            self._maybe_capture_prompt(data)
        try:
            if len(data) <= _WRITE_CHUNK_THRESHOLD:
                self._pty.write(data)
                return True
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
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"PTY {self.session_id[:8]} write failed: {exc}")
            return False

    def resize(self, rows: int, cols: int) -> None:
        rows = max(1, min(rows, 1000))
        cols = max(1, min(cols, 1000))
        self.rows = rows
        self.cols = cols
        if self._vt is not None:
            self._vt.resize(rows, cols)
        try:
            self._pty.setwinsize(rows, cols)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"PTY {self.session_id[:8]} resize failed: {exc}")

    def snapshot_frame(self) -> Optional[str]:
        """Render the current headless-VT frame (fullscreen agents only).

        ``None`` if this session has no VT mirror (non-fullscreen agent) or
        nothing has been fed to it yet — the caller falls back to the
        winsize-toggle repaint nudge in that case.
        """
        if self._vt is None:
            return None
        return self._vt.render()

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
        # Cancel any deferred submit still waiting to press Enter (#763) —
        # every stop mode writes to or tears down this PTY, so a watcher's
        # CR landing in the middle of an interrupt or a "/quit" sequence
        # would be answering a terminal state it never verified.
        self._defer_seq += 1
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
        # output_chars: cumulative-ish output signal (the scrollback ring's
        # length, so it saturates at _RING_MAX_CHARS). The board's dispatch
        # readiness probe (#302) only needs "has the agent painted anything
        # yet" — a monotonic counter would be overkill.
        with self._ring_lock:
            output_chars = len(self._ring)
        return {
            "session_id": self.session_id,
            "kind": self.kind,
            "agent": self.agent,
            "label": self.label,
            "project_dir": self.project_dir,
            "name": self.name,
            "flags": self.flags,
            "started_at": self.started_at,
            "alive": self.alive,
            "rows": self.rows,
            "cols": self.cols,
            "live_title": self.live_title,
            "prompt_title": self.prompt_title,
            "manual_title": self.manual_title,
            "output_chars": output_chars,
            # Raw PTY-activity timestamp (issue #627 remainder): exposed so a
            # future check can tell "still genuinely producing output" apart
            # from "live_title frozen on a busy glyph because the PTY wedged"
            # — not consumed anywhere yet, see board_transcript.py's
            # _live_title_is_busy docstring for why that's still open.
            "last_output_at": self._last_output_at,
            # The most recent server-initiated write's verdict (issue #760),
            # or None if nothing has been sent through /input yet. This is
            # what makes "this session has gone deaf to API input" a visible
            # state rather than something only a human with a keyboard can
            # tell apart from a busy agent.
            "last_input": self.last_input,
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
        self.manual_title = ""

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
                creationflags=NO_WINDOW,
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
            "manual_title": self.manual_title,
        }


# Environment variables a *parent* coding agent injects into the short-lived,
# non-interactive subprocesses it spawns for its own tool calls. They are
# correct there and actively wrong for the long-lived interactive agent we are
# about to host:
#
#   NO_COLOR                   strips every ANSI colour out of the child's TUI,
#                              so the session renders monochrome.
#   CLAUDE_CODE_CHILD_SESSION  makes Claude Code treat itself as a nested run
#                              and disable transcript saving — no .jsonl, no
#                              --resume, no history for the whole session.
#
# The rest are the parent's own identity, stale the moment we inherit it.
#
# They reach us whenever the tray was (re)started from inside an agent's tool
# subprocess — an agent running `tray.bat --restart` is the usual route — since
# the whole tray → webapp → session-host chain inherits that environment and we
# then hand our own os.environ to everything we spawn. Scrubbing here makes a
# launcher-hosted session independent of *how* the host happened to be started.
#
# Deliberately NOT scrubbed: user-scope settings.json vars that are meant for
# every agent run (CLAUDE_AUTOCOMPACT_PCT_OVERRIDE, telemetry), and
# GIT_TERMINAL_PROMPT — restoring git's interactive credential prompt could
# hang an agent's own git call on a prompt nobody is watching.
_INHERITED_AGENT_MARKERS = frozenset(
    {
        "AI_AGENT",
        "CLAUDECODE",
        "CLAUDE_CODE_BRIDGE_SESSION_ID",
        "CLAUDE_CODE_CHILD_SESSION",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_PID",
        "FORCE_COLOR",
        "NO_COLOR",
    }
)


def agent_child_env(session_id: str, agent: str) -> Dict[str, str]:
    """Build the environment for a launcher-spawned agent process.

    The host's own environment minus :data:`_INHERITED_AGENT_MARKERS`, plus
    this session's ``APP_LAUNCHER_SESSION_ID``/``APP_LAUNCHER_AGENT`` stamp,
    plus the **effective** ``PATH`` (issue #668).

    That last part is what lets ``cmd /c <exe>`` find a CLI installed after
    this host process started: the inherited block is frozen at spawn, so
    without it the child inherits a ``PATH`` that predates the install. It
    must be the *same* path :func:`src.agents.is_installed` resolves against
    — a button that lights up for a launch that then dies with "is not
    recognized as an internal or external command" is the worse failure.
    Only ``PATH`` is refreshed: the marker-scrubbing policy above is
    deliberate and stays exactly as it is.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in _INHERITED_AGENT_MARKERS
    }
    env["PATH"] = effective_path()
    env["APP_LAUNCHER_SESSION_ID"] = session_id
    env["APP_LAUNCHER_AGENT"] = agent
    return env


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
        history_lines: Optional[int] = None,
        label: str = "",
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

        ``history_lines`` bounds the headless-VT scrollback a full-screen
        agent's session mirrors for a (re)connect (issue #435 follow-up,
        user-configurable via Settings). ``None`` falls back to
        :class:`~src.vt_snapshot.VtSnapshot`'s own default. Ignored for a
        non-fullscreen agent (no VT mirror is created at all).
        """
        if PtyProcess is None:
            raise RuntimeError("pywinpty is not available — cannot spawn a PTY")
        if self._loop is None:
            raise RuntimeError("SessionManager has no event loop attached")
        _validate_flags(flags)
        directory = Path(project_dir)
        if not directory.is_dir():
            raise OSError(f"Project directory not found: {project_dir}")

        rows = max(1, min(int(rows), 1000))
        cols = max(1, min(int(cols), 1000))
        session_id = uuid.uuid4().hex
        # `cmd /c` resolves the agent command (e.g. claude.cmd / agy.cmd)
        # off PATH the way a normal shell would; when the agent exits, cmd
        # exits, the PTY closes, and the reader thread sees EOF.
        #
        # APP_LAUNCHER_SESSION_ID/AGENT ride the child's environment
        # (``env=``), NOT a `set "VAR=val" && ...` chain baked into the
        # command string (#537 root cause): PtyProcess.spawn() re-tokenizes a
        # str argv via shlex.split() then rebuilds it with
        # subprocess.list2cmdline(), which backslash-escapes the quotes
        # around "VAR=val" — cmd.exe's own SET parser doesn't strip that
        # escaping, so the vars silently never landed. create_remote()'s
        # PowerShell Start-Process path goes through a real cmd.exe shell
        # (no pywinpty re-tokenizing) and is unaffected by this.
        exe = command_for(agent)
        command = f"cmd /c {exe} {flags}".strip()
        child_env = agent_child_env(session_id, agent)
        pty = PtyProcess.spawn(
            command, cwd=str(directory), dimensions=(rows, cols), env=child_env
        )
        vt: Optional[VtSnapshot] = None
        if is_fullscreen(agent):
            vt = (
                VtSnapshot(rows, cols, history=history_lines)
                if history_lines is not None
                else VtSnapshot(rows, cols)
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
            label=label,
            rows=rows,
            cols=cols,
            _vt=vt,
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
        _validate_flags(flags)
        directory = Path(project_dir)
        if not directory.is_dir():
            raise OSError(f"Project directory not found: {project_dir}")
        session_id = uuid.uuid4().hex
        # `cmd /c` resolves the agent command (e.g. claude.cmd) off PATH and
        # closes the window when the agent exits — same shape as the PTY spawn.
        exe = command_for(agent)
        inner = (
            f'set "APP_LAUNCHER_SESSION_ID={session_id}" && '
            f'set "APP_LAUNCHER_AGENT={agent}" && '
            f"{exe} {flags}"
        ).strip()
        ps_command = (
            "(Start-Process -FilePath 'cmd' "
            f"-ArgumentList '/c {_ps_quote(inner)}' "
            f"-WorkingDirectory '{_ps_quote(str(directory))}' -PassThru).Id"
        )
        # The console inherits its environment through this PowerShell, so the
        # scrub has to happen here too — otherwise a detached session gets the
        # same monochrome, transcript-less child a PTY session would.
        result = subprocess.run(
            [_POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", ps_command],
            capture_output=True,
            text=True,
            creationflags=NO_WINDOW,
            timeout=30,
            env=agent_child_env(session_id, agent),
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

    def rename(self, session_id: str, title: str) -> Optional[Any]:
        """Set (or, with an empty ``title``, clear) a manual title override.

        The ``manual_title`` override works identically for
        :class:`PtySession` and :class:`RemoteSession` — the one title channel
        that reaches a detached session (issue #458), and the only one the
        launcher needs: it names the session in the UI, the window title, the
        Coding tab, and the Board without any PTY involvement. (Forwarding the
        rename into the agent's own CLI — #503 — was removed in #555 as
        unfixably racy against the live TUI; see that issue.)
        """
        session = self.get(session_id)
        if session is None:
            return None
        session.manual_title = title.strip()[:_MANUAL_TITLE_MAX_CHARS]
        return session

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
