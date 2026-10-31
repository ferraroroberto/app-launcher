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
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO, Tuple

from src.agents import (
    DEFAULT_AGENT,
    command_for,
    is_fullscreen,
    is_safe_command_text,
    quit_command_for,
)
from src.audit import transcript_path
from src.session_host_composer import ComposerTracker, composer_tracker_for
from src.session_host_input import touches_composer
from src.session_hosting.child_env import agent_child_env
from src.session_hosting.deferred_submit import DeferredSubmit
from src.session_hosting.kinds import (
    _STOP_GRACE_SECONDS,
    KIND_PTY,
    STOP_INTERRUPT,
    STOP_KILL,
    STOP_QUIT,
)
from src.session_hosting.remote import RemoteSession, launch_detached
from src.session_host_scan import (  # noqa: F401 — re-exported for callers/tests
    _PROMPT_TITLE_MAX_CHARS,
    _PROMPT_TITLE_MAX_WORDS,
    _cook_input_line,
    _derive_prompt_title,
    _parse_osc_title,
    _scan_bracketed_paste_mode,
    _strip_color_osc,
)
from src.vt_snapshot import VtSnapshot

try:  # Windows-only — ConPTY via pywinpty.
    from winpty import PtyProcess  # type: ignore
except ImportError:  # pragma: no cover — non-Windows / missing dep
    PtyProcess = None  # type: ignore

logger = logging.getLogger(__name__)

# How much terminal output to keep per session for scrollback-on-reconnect.
_RING_MAX_CHARS = 256 * 1024
# After a hard ring truncation, advance the head to the next newline within
# this window so a replay never starts mid-escape-sequence — a cut
# inside a CSI/OSC renders its tail as literal garbage at the top of the
# replayed scrollback. One rendered line (with its SGR decoration) is far
# shorter than this; if no newline appears in the window, keep the raw cut
# rather than discard more history.
_RING_TRIM_SCAN = 4096
# Chunk size for the blocking PTY read in the reader thread.
_READ_CHUNK = 4096
# Sentinel pushed to subscribers when the session ends.
_EOF = object()

# Chunk-and-pace thresholds for writes into the ConPTY input pipe.
# A real-PTY readback harness (test_session_host_pty_realpty.py) showed the
# write boundary itself delivers multi-KB payloads losslessly — pywinpty's
# write into the input pipe is effectively blocking and does NOT drop the
# tail. So chunking is not a truncation fix (the original framing was
# wrong); it is pacing, which keeps a multi-KB burst from overrunning the
# Windows console input queue that a busy TUI drains slowly. The agent-side
# atomic-paste fix (bracketed-paste framing) lives client-side in
# terminal.js (framePaste). We keep small writes one-shot and split larger
# payloads into ~512 B chunks with a small pause; pywinpty's return value is
# never interpreted as a bytes-accepted count — doing so amplified a single
# keystroke into thousands.
_WRITE_CHUNK_THRESHOLD = 512
_WRITE_CHUNK_SIZE = 512
_WRITE_CHUNK_PAUSE = 0.003

# Smallest PTY size a resize may set. Every resize SIGWINCHes an
# inline agent (Claude Code) into a full-viewport repaint that leaves the
# previous copy in the client's scrollback, and the iOS keyboard sweep samples
# 1- and 6-row sizes mid-animation that are never a real viewport. The phone
# floors its frames to the same values (terminal.js PTY_MIN_*); this clamp
# covers every other caller (the WS frame, POST /sessions/{sid}/resize).
PTY_MIN_ROWS = 8
PTY_MIN_COLS = 20

# The server-initiated input-delivery protocol — bracketed-paste framing,
# bulk settle-then-submit and ingest/echo verification — lives in
# :mod:`src.session_host_input`, and the deferred-submit watcher that extends
# it in :mod:`src.session_hosting.deferred_submit`. Only
# :class:`DeferredSubmit` is imported here (as ``PtySession``'s base);
# callers/tests needing the protocol's constants or :class:`InputOutcome`
# import them from those modules directly.

# First-prompt session title: how much un-submitted input we
# buffer while waiting for the first submit — a line the user never sends
# must not grow unbounded. This is session *state* (the buffer lives on
# ``PtySession``), unlike the deterministic derivation itself
# (``_derive_prompt_title``/``_PROMPT_TITLE_MAX_CHARS``/``_PROMPT_TITLE_MAX_WORDS``),
# which is a pure function and lives in :mod:`src.session_host_scan`.
_PROMPT_TITLE_BUF_MAX = 400

# Manual title override: a launcher-native rename that wins over
# every auto-derived title (live_title/prompt_title/shared_name — see
# sessions.js::sessionTitle) and, unlike those, also reaches a detached
# RemoteSession — the one rename channel that works identically across all
# launcher-supported agents without depending on agent-native OSC support.
# Kept in-memory on the session object (like live_title/prompt_title) rather
# than persisted: it dies with the session, so it needs no cleanup/pruning.
_MANUAL_TITLE_MAX_CHARS = 60

# Beat between the three keystrokes STOP_QUIT types. The quit
# command is ESC, then "/quit", then the submitting CR — three separate
# writes with this pause between them, never one burst. Two distinct races
# make the burst wrong, and both were measured through the real launcher
# path against all six agents, reading each harness's own composer *before*
# any CR (so no model turn was spent):
#
#   1. ESC immediately followed by "/" is ambiguous at the terminal layer:
#      a TUI that binds Alt-keys reads "ESC <char>" arriving together as one
#      meta-key press, and swallows the slash. Pi 0.84.4, Antigravity 1.2.5
#      and Copilot 1.0.86 all do; the harness then sees a bare "quit" and
#      answers it as a prompt (Pi's threshold measured here: eaten at 25 ms,
#      intact at 50 ms). Claude and Grok happen not to — which is why this
#      looked like a two-harness quirk rather than the protocol-level
#      ambiguity it is.
#   2. The command and its CR in one write race the slash-command popup:
#      Codex 0.154.0 leaves "/quit" sitting in an open popup and never acts
#      on the CR, so *every* Codex stop fell through to the force-terminate
#      below. This is the same lesson submit_input learned —
#      the submitting CR is always its own write.
#
# A fixed pause, not submit_input's quiet-window settle: ESC produces little
# or no output, so waiting for quiet would return before the harness's
# escape-timeout expired and fix nothing. 250 ms clears every threshold
# measured (and the ~100 ms xterm/crossterm/bubbletea defaults) with room to
# spare, and the two beats together cost 0.5 s of the 5 s grace window.
_STOP_KEY_SETTLE_SECONDS = 0.25

# The allowlist for the ``flags`` string SessionManager.create/create_remote
# splice straight into ``cmd /c <exe> <flags>`` (create) or a PowerShell
# ``Start-Process -ArgumentList`` (create_remote) — the boundary that
# actually owns the command line. A superset of
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
class PtySession(DeferredSubmit):
    """One ``claude`` process running inside a launcher-owned ConPTY.

    The server-initiated input-delivery protocol (``submit_input`` and
    everything downstream of it — settle-then-submit, ingest/echo
    verification, the deferred-submit watcher) is supplied by
    :class:`~src.session_host_input.InputProtocol` and
    :class:`~src.session_hosting.deferred_submit.DeferredSubmit`, not defined here; this
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
    # Free-form role tag set at create time (e.g. "chief") so callers
    # can find a purpose-built session deterministically; "" for normal ones.
    label: str = ""
    rows: int = 40
    cols: int = 120
    _ring: str = ""
    _ring_lock: threading.Lock = field(default_factory=threading.Lock)
    # Serializes the *input* side. ``_ring_lock`` guards output
    # only, and the manager's own lock guards the sessions registry — neither
    # covers the PTY write path, which has had two concurrent writers since
    # the HTTP ``/sessions/{sid}/input`` route was added alongside the
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
    # Headless VT mirror for full-screen (ratatui) agents only
    # — Claude's raw-ring replay path never needs one. None for a
    # non-fullscreen agent, or before SessionManager wires it in.
    _vt: Optional["VtSnapshot"] = None
    # Settle-then-submit state for server-initiated writes.
    # _bracketed_paste_mode mirrors xterm's term.modes.bracketedPasteMode,
    # tracked off the PTY's own DECSET 2004 output (default False — matches
    # the client-side gate's own default before any signal has been seen).
    # _last_output_at mirrors terminal-compose.js's t.lastOutputAt, used by
    # the bulk-settle wait to detect the paste's ingest has gone quiet.
    _bracketed_paste_mode: bool = False
    _decset_carry: str = ""
    _last_output_at: float = 0.0
    # Monotonic count of every character ever pumped out of this PTY. Unlike
    # to_api()'s output_chars — the ring's length, which
    # saturates at _RING_MAX_CHARS — this keeps counting, so a write can mark
    # a position and later ask "what has the terminal painted since?" even on
    # a long-lived session whose ring has wrapped many times over.
    _output_total: int = 0
    # The most recent server-initiated write's outcome, surfaced
    # in to_api() so a session that has gone deaf to API input is visible as
    # such instead of being indistinguishable from a busy agent.
    last_input: Optional[Dict[str, Any]] = None
    # Deferred-submit generation counter. Every new
    # server-initiated write — and every stop — bumps it, which supersedes any
    # watcher still waiting to press Enter: its "my payload is the thing
    # sitting unsent in the composer" premise no longer holds once somebody
    # else has written to the PTY. A superseded watcher exits without firing
    # and without touching ``last_input``, which by then belongs to the newer
    # call. Raw keystrokes from the WebSocket pump deliberately do *not* bump
    # it — a PC mirror typing anywhere would otherwise cancel every steer, and
    # the pre-fire re-check already handles a composer the human has changed.
    # That re-check is what carries the weight now the watcher's window is long
    # enough to outlast a gate run, so it identifies *which* payload is
    # sitting there rather than merely that one is — see
    # ``session_host_input._chip_visible``.
    _defer_seq: int = 0
    # Set by _submit_input_locked when it hands a submit to the watcher, read
    # (and cleared) by submit_input once the ``deferred`` verdict is recorded,
    # so the watcher can never record its own outcome first.
    # ``(seq, mark, needles, chip_id, touches)`` — chip_id is the paste or
    # image chip pinned at ingest, or None when the composer
    # echoed the payload verbatim instead of collapsing it into a chip;
    # touches is ``_composer_touches`` at ingest.
    _defer_args: Optional[Tuple[int, int, List[str], Optional[str], int]] = None
    # Keystrokes that can change what the composer holds:
    # ``write()`` — the keyboard path — bumps it for any write carrying a
    # control key (Enter, Backspace, Ctrl-C, Esc, arrows, a paste), while
    # plain typed text and the terminal's own query replies leave it alone.
    # Nothing leaves the composer without one, so a deferred watcher whose
    # count is unchanged knows its payload is still sitting there — a fact a
    # differential renderer that never repaints an unchanged composer cannot
    # show it. ``submit_input`` writes through ``_write_locked`` and is
    # counted by ``_defer_seq`` instead.
    _composer_touches: int = 0
    # Turn state read off the agent's own screen, kept by the
    # reader thread (``InputProtocol._track_turn_state``). ``_bg_waiting`` is
    # True while the main agent sits idle at its prompt with background
    # agents running — the one state that is ready for a submit yet never
    # goes quiet. ``_main_turn_at`` is the ``_output_total`` reading at the
    # last main-turn spinner, which is how the deferred watcher confirms its
    # CR started a turn. ``_main_turn_seen_at`` is the wall time of that same
    # spinner: a recent one means the main turn is running right now,
    # which is when a type-ahead agent queues an Enter.
    _bg_waiting: bool = False
    _main_turn_at: int = 0
    _main_turn_seen_at: float = 0.0
    # Whether the composer holds typed-but-unsent text, fed by
    # every write in ``_write_locked``. None for an agent whose composer was
    # never probed, which reports ``composer_dirty: None``.
    _composer: Optional[ComposerTracker] = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._composer = composer_tracker_for(self.agent)

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
            # Strip OSC 10/11/12 colour query/reply sequences at the
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
            # real terminal client would.
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
            self._track_turn_state(chunk)
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
            if touches_composer(data):
                self._composer_touches += 1
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
            else:
                # Long write (paste / large input): pace it in ~512 B chunks
                # so the burst doesn't overrun the console input queue a busy
                # TUI drains slowly. pywinpty's return value is
                # deliberately ignored — interpreting it as a bytes-accepted
                # count amplified a single keystroke into thousands.
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
            if self._composer is not None:
                # Some of the bytes may have landed before the raise.
                self._composer.mark_unknown()
            return False
        if self._composer is not None:
            self._composer.feed(data)
        return True

    def resize(self, rows: int, cols: int) -> None:
        rows = max(PTY_MIN_ROWS, min(rows, 1000))
        cols = max(PTY_MIN_COLS, min(cols, 1000))
        if (rows, cols) == (self.rows, self.cols):
            # Nothing to change — a same-size setwinsize would only cost a
            # round-trip, and must never be what repaints the agent.
            return
        logger.info(
            f"↔️ PTY {self.session_id[:8]} resize "
            f"{self.rows}x{self.cols} -> {rows}x{cols}"
        )
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
        self,
        mode: str = STOP_QUIT,
        grace_seconds: float = _STOP_GRACE_SECONDS,
        key_settle_seconds: float = _STOP_KEY_SETTLE_SECONDS,
    ) -> None:
        """Stop the session: graceful agent-own quit, then force-fallback.

        ``STOP_QUIT`` (the path the single "Stop and kill" button drives)
        types the agent's *own* quit command — Claude's ``/quit``,
        Copilot's ``/exit``, … (see :func:`quit_command_for`) — after an
        ESC that clears any partial prompt, then waits up to
        ``grace_seconds`` for the agent to exit on its own. Those are three
        keystrokes, not one burst: ESC, the command, the submitting CR, with
        ``key_settle_seconds`` between them so neither the ESC nor the
        command runs into what follows it (see
        ``_STOP_KEY_SETTLE_SECONDS``, issue #1016). The clean exit
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
        # Cancel any deferred submit still waiting to press Enter —
        # every stop mode writes to or tears down this PTY, so a watcher's
        # CR landing in the middle of an interrupt or a "/quit" sequence
        # would be answering a terminal state it never verified.
        self._defer_seq += 1
        if mode == STOP_INTERRUPT:
            if self._composer is not None:
                # An interrupt never went through write(), and an interrupted
                # turn can put its prompt back in the composer.
                self._composer.mark_unknown()
            try:
                self._pty.sendintr()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"PTY {self.session_id[:8]} interrupt failed: {exc}")
            return  # not a termination — leave the session (and mirror) alive

        try:
            if mode == STOP_KILL:
                self._pty.terminate(force=True)
            else:  # STOP_QUIT — graceful, agent-appropriate, with fallback.
                # Three keystrokes, each its own write, with a beat between:
                # ESC clears any partial input so the quit
                # command lands on an empty prompt; the beat after it stops
                # the leading "/" being read as part of a meta-key sequence;
                # the beat before the CR lets the harness's slash-command
                # popup open so the CR submits the command instead of being
                # swallowed. Bytes and order are unchanged — only the gaps
                # are new.
                self._pty.write("\x1b")
                time.sleep(key_settle_seconds)
                self._pty.write(quit_command_for(self.agent))
                time.sleep(key_settle_seconds)
                self._pty.write("\r")
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
        # readiness probe only needs "has the agent painted anything
        # yet" — a monotonic counter would be overkill.
        with self._ring_lock:
            output_chars = len(self._ring)
        composer_dirty, composer_dirty_since = (
            self._composer.state() if self._composer is not None else (None, None)
        )
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
            # Raw PTY-activity timestamp: lets a reader tell
            # "still genuinely producing output" apart from "live_title frozen
            # on a busy glyph because the PTY wedged". Consumed by
            # src.board.transcript's _pty_output_is_fresh (via src.board.sessions
            # and the session_transcript router).
            "last_output_at": self._last_output_at,
            # The most recent server-initiated write's verdict,
            # or None if nothing has been sent through /input yet. This is
            # what makes "this session has gone deaf to API input" a visible
            # state rather than something only a human with a keyboard can
            # tell apart from a busy agent.
            "last_input": self.last_input,
            # Typed-but-unsent text in the composer: True,
            # False, or None when it can't be established — see
            # src/session_host_composer.py. A typer holds off on True and
            # None alike.
            "composer_dirty": composer_dirty,
            "composer_dirty_since": composer_dirty_since,
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

        # The same floors :meth:`PtySession.resize` applies, as the docstring
        # above promises. `POST /sessions` forwards the request's rows
        # unfloored (`int(body.get("rows") or 40)` only catches a falsy value,
        # so a literal 1 survives), and a 1-row ConPTY's first real resize
        # fires exactly the full-viewport repaint PTY_MIN_ROWS exists to
        # prevent.
        rows = max(PTY_MIN_ROWS, min(int(rows), 1000))
        cols = max(PTY_MIN_COLS, min(int(cols), 1000))
        session_id = uuid.uuid4().hex
        # `cmd /c` resolves the agent command (e.g. claude.cmd / agy.cmd)
        # off PATH the way a normal shell would; when the agent exits, cmd
        # exits, the PTY closes, and the reader thread sees EOF.
        #
        # APP_LAUNCHER_SESSION_ID/AGENT ride the child's environment
        # (``env=``), NOT a `set "VAR=val" && ...` chain baked into the
        # command string: PtyProcess.spawn() re-tokenizes a
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
        exe = command_for(agent)
        pid = launch_detached(directory, session_id, agent, exe, flags)
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

    def reap_dead(self) -> List[Any]:
        """Drop sessions whose process has exited. Returns the sessions
        reaped, so the caller can clean up after them (their large uploads,
        #1430)."""
        with self._lock:
            dead = [sid for sid, s in self._sessions.items() if not s.alive]
            return [self._sessions.pop(sid) for sid in dead]

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
