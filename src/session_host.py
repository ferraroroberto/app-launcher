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
import re
import subprocess
import threading
import time
import uuid
from collections import deque
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

# Settle-then-submit for bulk input (issue #611) — ported from
# app/webapp/static/terminal-compose.js's sendSubmit/bulkSettle (#166/#499),
# the WS/compose-bar path's fix for the same race the HTTP /input endpoint
# lacked: a bulk (dictation-sized) paste's bracketed-paste ingest can outrun
# a fixed delay under machine load, so a CR arriving too soon lands mid-ingest
# and becomes a literal newline into the still-settling composer instead of
# Submit, leaving the message stranded as an unsent "[Pasted text #N]" chip.
# These four values are copied verbatim from terminal-compose.js so the two
# paths cannot drift and quietly diverge again — see that file's #499 comment
# for the calibration data (echo-then-quiet was the only protocol that ran
# clean 20/20 against a live ConPTY probe under synthetic load).
_BULK_SUBMIT_THRESHOLD_CHARS = 500
_BULK_FLOOR_MS = 350
_BULK_QUIET_MS = 350
_BULK_CAP_MS = 3000
# Poll interval while waiting for the bulk-settle condition — matches the
# 50ms setInterval terminal-compose.js polls at.
_BULK_POLL_S = 0.05

# Ingest verification for a server-initiated bulk write (issue #760). The
# settle protocol above answers "has the terminal gone quiet", which is not
# the same question as "did my payload actually land": three workers went
# deaf to API input on 2026-08-14 while `_pty.write` kept returning without
# raising, so the endpoint answered {"ok": true} for messages that were
# never submitted (transcript evidence: the composer holding an unsent
# "[Pasted text #N]" chip while the agent worked on). A busy agent never
# goes quiet — its spinner repaints every few tens of ms — so the settle
# wait always ran out to _BULK_CAP_MS and fired the CR blind, mid-repaint,
# where Claude Code absorbs it as a newline instead of Submit.
#
# So before submitting we look for the payload in the PTY's *own output*:
# either the text echoed back, or the "[Pasted text #N]" chip Claude Code
# collapses a bulk paste into. No evidence → the payload demonstrably did
# not reach the terminal, so the CR is NOT written (a blind CR into an
# unknown TUI state is worse than no CR: with a modal dialog open — the
# other state observed in the 2026-08-14 transcripts — it selects a menu
# option) and the caller is told so, loudly.
#
# _INGEST_CAP_MS extends only the no-evidence-yet wait (under load the echo
# itself is deferred — see terminal-compose.js's #499 note), and stays well
# inside session_client's own input timeout so the caller sees this verdict
# rather than a client-side read timeout.
_INGEST_CAP_MS = 5000
# How much of the ring's newly-appended tail to normalize and scan.
_ECHO_SCAN_CHARS = 65536
# Normalized payload fragments used as echo needles: long enough not to
# collide with ordinary terminal furniture, short enough to survive the
# composer wrapping and decorating a long paste — and sampled across the
# payload so one mangled fragment doesn't hide the whole match.
_ECHO_FRAGMENT_CHARS = 24
_ECHO_SAMPLES = 4
# Claude Code's collapsed-paste chip, normalized (see _normalize_echo) —
# positive ingest evidence for a payload that is never echoed verbatim.
_PASTE_CHIP_MARKER = "[pastedtext#"

# Deferred submit (issue #763, closing #760's third acceptance point: "if the
# terminal can take input, so can the API"). A *working* agent repaints its
# spinner every few tens of ms, so the settle wait above can never see a quiet
# window for as long as it works: it runs out to _BULK_CAP_MS with the payload
# ingested but still sitting unsent in the composer. Firing the CR there —
# what this module did before #763 — is the 2026-08-14 stall shape exactly:
# mid-repaint, Claude Code absorbs a CR as a literal newline.
#
# A human at the keyboard does not hit this. They see the stranded
# "[Pasted text #N]" chip, wait for the agent to settle, and press Enter. The
# watcher below is that retry-by-observation and nothing more: it re-presses
# Enter, and it never resends the *text* (a blind resend of something like
# "/issue-finish" could double-execute — the same reason chief_ops.py's
# `say --verify` deliberately has no auto-retry).
#
# _DEFER_QUIET_MS is deliberately far longer than _BULK_QUIET_MS: 350 ms only
# has to outlast one paste's ingest, whereas this has to outlast the repaint
# gaps of a working agent and actually mean "the turn is over".
_DEFER_QUIET_MS = 1500
# Bounded: past this the steer stays stranded and honestly reported, which is
# the pre-#763 behaviour. An unbounded watcher would keep a CR primed against
# a terminal whose state drifted arbitrarily far from the one it verified.
_DEFER_CAP_MS = 120_000
_DEFER_POLL_S = 0.25
# How far back the pre-fire re-check looks for the payload. The composer is
# repainted on essentially every frame, so a payload still sitting unsent
# keeps reappearing, whereas one that was submitted (or scrolled away behind
# a dialog) stops being repainted and only survives in older scrollback.
# Sampling _output_total on every poll and scanning from the oldest retained
# sample is what turns "the payload was there once" into "it is there now".
_DEFER_FRAME_LOOKBACK_MS = 6000
_DEFER_FRAME_SAMPLES = max(2, int(_DEFER_FRAME_LOOKBACK_MS / 1000 / _DEFER_POLL_S))
# Defence in depth for the one case the positive check above cannot rule out
# by itself: a permission / AskUserQuestion dialog painted *over* a composer
# that still shows the chip, where a bare CR picks a menu option instead of
# submitting (the second terminal state seen in the 2026-08-14 transcripts).
# Normalized (see _normalize_echo) markers for the dialog shapes Claude Code
# paints. This is a heuristic and is allowed to be over-eager: a false
# positive only means the watcher declines to press Enter, leaving the steer
# stranded and honestly reported — never a CR into a dialog.
_DEFER_DIALOG_MARKERS = ("doyouwanttoproceed", "❯1.", "❯2.")

# Outcome reasons for a server-initiated write (issue #760). Distinct
# conditions get distinct reasons — "couldn't establish delivery" is never
# folded into the success state.
INPUT_OK = "ok"                        # landed; submit (if asked) confirmed
INPUT_UNVERIFIED = "unverified"        # short payload: submitted un-verified
INPUT_NOT_INGESTED = "not_ingested"    # written, never echoed → not delivered
INPUT_DROPPED = "dropped"              # a write never reached the PTY
INPUT_NOOP = "noop"                    # nothing was asked for
# Deferred-submit reasons (issue #763). INPUT_DEFERRED is what the /input
# call itself returns once the watcher is armed; the other three are the
# watcher's own terminal verdicts, recorded onto ``last_input`` when it
# finishes. It replaces the former "settle_cap" reason, which reported a CR
# that had already been fired blind — there is no longer such a CR to report.
INPUT_DEFERRED = "deferred"            # ingested; submit handed to the watcher
INPUT_DEFER_TIMEOUT = "defer_timeout"  # never went quiet within _DEFER_CAP_MS
INPUT_DEFER_VANISHED = "defer_vanished"  # quiet, but the payload is gone
INPUT_DEFER_UNCLEAR = "defer_unclear"  # quiet, payload there, but a dialog too

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


# Any escape sequence a TUI paints its composer with — CSI (cursor moves,
# SGR), OSC (titles), and the two-char ESC forms. Stripped before matching a
# payload against the terminal's own output, since a wrapped, re-decorated
# echo of "CHIEF - do X" is interleaved with dozens of these.
_ECHO_ESCAPE_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-_]"
)
# Whitespace and box-drawing runs: the two kinds of noise a TUI sprays
# *through* an echoed payload. A bordered composer paints "│ text │" on
# every wrapped line, so the border characters land inside the text as
# reliably as the wrap itself does. Both sides of the comparison get the
# same treatment, so a payload that genuinely contains box-drawing still
# matches itself.
_ECHO_NOISE_RE = re.compile(r"[\s─-╿]+")


def _normalize_echo(text: str) -> str:
    """Escape-free, whitespace-free, lowercased form of terminal text.

    Dropping *all* whitespace and border decoration is what makes an echo
    match survive the composer: the same payload comes back hard-wrapped at
    the terminal width, re-indented inside a box-drawn frame, and split
    across repaints, so any newline- or column-sensitive comparison misses
    it. What survives that treatment is the sequence of content characters.
    """
    return _ECHO_NOISE_RE.sub("", _ECHO_ESCAPE_RE.sub("", text)).lower()


def _echo_needles(data: str) -> List[str]:
    """Normalized fragments of ``data`` to look for in the PTY's output.

    Several fragments sampled across the payload rather than one, because
    any single one can be broken up by decoration this normalizer doesn't
    know about (an agent that numbers wrapped lines, a truncation ellipsis
    in the middle of a collapsed paste). A match on *any* of them is
    evidence the payload reached the terminal.

    Empty list when the payload normalizes to less than one fragment's
    worth of content — the caller treats that as "nothing to match on"
    rather than as a failed match.
    """
    normalized = _normalize_echo(data)
    if len(normalized) < _ECHO_FRAGMENT_CHARS:
        return []
    step = max(1, (len(normalized) - _ECHO_FRAGMENT_CHARS) // _ECHO_SAMPLES)
    return [
        normalized[i : i + _ECHO_FRAGMENT_CHARS]
        for i in range(0, len(normalized) - _ECHO_FRAGMENT_CHARS + 1, step)
    ][:_ECHO_SAMPLES]


@dataclass(frozen=True)
class InputOutcome:
    """What actually happened to one server-initiated write (issue #760).

    ``reason`` is the single source of truth; the booleans are the detail a
    caller renders. ``ingested``/``submit_confirmed`` are tri-state on
    purpose — ``None`` means *not established*, which is never the same as
    ``False`` and never folded into the success state.
    """

    reason: str
    ingested: Optional[bool] = None
    submitted: bool = False
    submit_confirmed: Optional[bool] = None
    waited_ms: int = 0
    # Whether the deferred-submit watcher (issue #763) is involved: True on
    # the ``deferred`` verdict the /input call returns once the watcher is
    # armed, and on every verdict the watcher itself later records. Lets a
    # reader tell "the submit is still coming" apart from a finished call.
    deferred: bool = False

    @property
    def delivered(self) -> bool:
        """Whether everything this call attempted actually reached the PTY.

        ``INPUT_NOT_INGESTED`` counts as *not* delivered: the write returned
        without raising, but the terminal never showed the payload, which is
        exactly the silent drop #760 was filed over. The deferred-submit
        verdicts (#763) all stay *delivered*: the payload demonstrably
        reached the composer — it is only the submitting CR that is pending,
        withheld, or refused, and ``submit_confirmed`` says which.
        """
        return self.reason not in (INPUT_DROPPED, INPUT_NOT_INGESTED)

    def to_api(self) -> Dict[str, Any]:
        return {
            "delivered": self.delivered,
            "reason": self.reason,
            "ingested": self.ingested,
            "submitted": self.submitted,
            "submit_confirmed": self.submit_confirmed,
            "waited_ms": self.waited_ms,
            "deferred": self.deferred,
        }


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

    def _normalized_since(self, mark: int) -> str:
        """Normalized form of everything the PTY has painted since ``mark``.

        ``mark`` is an ``_output_total`` reading, so the window is defined by
        the monotonic output counter rather than by ring offsets — it stays
        correct across a ring that has wrapped. Empty string when nothing has
        been painted since.
        """
        with self._ring_lock:
            appended = self._output_total - mark
            if appended <= 0:
                return ""
            window = self._ring[-min(appended, len(self._ring), _ECHO_SCAN_CHARS):]
        return _normalize_echo(window)

    @staticmethod
    def _payload_visible(normalized: str, needles: List[str]) -> bool:
        """Does ``normalized`` terminal output show this payload?

        Evidence is either the payload itself coming back (normalized, so
        wrapping and box-drawing don't hide it) or Claude Code's
        collapsed-paste chip, which replaces the echo for a bulk paste.
        """
        if _PASTE_CHIP_MARKER in normalized:
            return True
        return any(needle in normalized for needle in needles)

    def _echo_seen_since(self, mark: int, needles: List[str]) -> bool:
        """Has the PTY painted evidence of this payload since ``mark``?

        Only output the terminal produced *after* ``mark`` counts — an
        identical payload sent earlier in the session can't be mistaken for
        this one's echo.
        """
        return self._payload_visible(self._normalized_since(mark), needles)

    def submit_input(self, data: str, submit: bool) -> "InputOutcome":
        """Write ``data`` and, if ``submit``, follow it with a submitting CR.

        Ported from ``app/webapp/static/terminal-compose.js``'s
        ``framePaste``/``sendSubmit``/``bulkSettle`` (issues #166/#450/#499)
        for the HTTP ``/input`` path (issue #611), which previously wrote the
        (unconditionally bracketed) text and the CR back-to-back with no
        settle logic — Claude Code's composer classifies a bulk write as a
        paste, and a CR arriving mid-ingest is absorbed as a literal newline
        instead of Submit, stranding the message as an unsent
        ``[Pasted text #N]`` chip while the caller was told ``ok: true``.

        - Bracketed-paste framing (``\\x1b[200~ … \\x1b[201~``) is applied
          only when ``self._bracketed_paste_mode`` is currently on (tracked
          from the PTY's own DECSET 2004 output — see
          ``_scan_bracketed_paste_mode``), matching ``framePaste``'s gate on
          xterm's client-side ``term.modes.bracketedPasteMode`` exactly: a
          literal ``\\x1b[200~`` sent to an agent that never asked for it is
          garbage, not a paste.
        - The CR is always its own separate ``write()`` call, never
          concatenated onto the framed text (#166).
        - ``data`` blank + ``submit`` → a bare submit against whatever is
          already sitting in the composer, with no text write at all — the
          escape hatch for a message already stranded by this exact race
          (previously both ``{"data": "", "submit": true}`` and any other
          blank-data call 400'd, leaving no recovery but a human tapping the
          phone's own Send control).
        - Payloads at/above ``_BULK_SUBMIT_THRESHOLD_CHARS`` hold the CR
          until the session's output stream shows the paste was echoed and
          has gone quiet (floor/quiet/cap — #499); shorter payloads submit
          immediately, matching ``sendSubmit``'s "short sends stay instant".
        - A bulk payload is additionally checked for *ingest evidence*
          before the CR is written at all (issue #760): the terminal must
          have painted the payload back (or its ``[Pasted text #N]`` chip)
          since the write. No evidence within ``_INGEST_CAP_MS`` → the CR is
          withheld and the outcome says ``not_ingested``, because a blind CR
          into a terminal that never showed the text can do real damage (it
          answers whatever modal dialog is open).
        - A bulk payload that *was* ingested but whose stream never goes
          quiet — a busy agent, the 2026-08-14 shape — no longer gets a blind
          CR at the settle cap either (issue #763). The submit is handed to a
          bounded background watcher instead, the call returns ``deferred``,
          and ``_write_lock`` is released immediately so a keyboard writer is
          never blocked behind the wait. See :meth:`_await_deferred_submit`.

        Returns an :class:`InputOutcome` describing what actually happened —
        superseding the old bare ``bool`` (#607), which could only say "the
        write didn't raise" and so reported ``True`` for every silently
        stranded steer in #760's three occurrences.

        The whole sequence runs under ``_write_lock`` (issue #721): the CR is
        a separate PTY write from the text it submits, so without the lock a
        concurrent writer — the WebSocket pump serving a PC mirror on the
        same session — can land its own bytes *between* them and turn a
        submit into a literal newline in somebody else's message. The
        ingest wait extends the hold to at most ``_INGEST_CAP_MS``, and only
        on the failing path, so a keyboard writer is never blocked for long.
        """
        with self._write_lock:
            outcome = self._submit_input_locked(data, submit)
            defer_args = self._defer_args
            self._defer_args = None
        self._record_input(outcome, len(data), submit)
        # Armed only after the ``deferred`` verdict is already on the session,
        # so the watcher can never overwrite ``last_input`` with its own
        # outcome before the call that created it has recorded one.
        if defer_args is not None:
            self._arm_deferred_submit(*defer_args, nbytes=len(data))
        return outcome

    def _record_input(
        self, outcome: "InputOutcome", nbytes: int, submit: bool
    ) -> None:
        """Record one server-initiated write's verdict and log a breadcrumb.

        Shared by :meth:`submit_input` and the deferred-submit watcher (#763),
        so a deferred outcome lands on ``last_input`` in exactly the same
        shape as an immediate one and ``to_api()`` needs no special case.
        """
        self.last_input = {
            **outcome.to_api(),
            "at": time.time(),
            "bytes": nbytes,
            "submit": submit,
        }
        # Breadcrumbs for the next occurrence (#760): the whole defect was
        # invisible in the logs, so both an outright non-delivery and an
        # unconfirmed submit — the shape all three 2026-08-14 stalls took —
        # get an INFO line naming which condition it was.
        sid = self.session_id[:8]
        if not outcome.delivered:
            logger.info(
                f"⚠️ PTY {sid} input not delivered "
                f"({outcome.reason}, {nbytes} chars, "
                f"waited {outcome.waited_ms}ms)"
            )
        elif outcome.reason == INPUT_DEFERRED:
            logger.info(
                f"⏳ PTY {sid} input ingested but the agent is still busy — "
                f"submit deferred to a watcher ({nbytes} chars, waited "
                f"{outcome.waited_ms}ms, window {_DEFER_CAP_MS}ms)"
            )
        elif outcome.deferred and outcome.reason == INPUT_OK:
            logger.info(
                f"✅ PTY {sid} deferred submit landed after "
                f"{outcome.waited_ms}ms ({nbytes} chars)"
            )
        elif outcome.submit_confirmed is False:
            logger.info(
                f"ℹ️ PTY {sid} input ingested but not submitted "
                f"({outcome.reason}, {nbytes} chars, waited "
                f"{outcome.waited_ms}ms)"
            )

    def _submit_input_locked(self, data: str, submit: bool) -> "InputOutcome":
        """:meth:`submit_input`'s body, assuming ``_write_lock`` is held."""
        # Any new server-initiated write supersedes a watcher still waiting to
        # press Enter for an earlier payload (#763): the composer no longer
        # holds only that payload, so the watcher's pre-fire re-check would be
        # reasoning about a terminal state that has already moved on.
        self._defer_seq += 1
        if not data:
            if not submit:
                return InputOutcome(reason=INPUT_NOOP)
            if not self._write_locked("\r"):
                return InputOutcome(reason=INPUT_DROPPED)
            # A bare submit carries no payload to look for, so there is
            # nothing to verify — it releases whatever the composer already
            # holds. Honest name for that: unverified, not "ok".
            return InputOutcome(reason=INPUT_UNVERIFIED, submitted=True)
        framed = "\x1b[200~" + data + "\x1b[201~" if self._bracketed_paste_mode else data
        mark = self._output_total
        sent_at = time.time()
        if not self._write_locked(framed):
            return InputOutcome(reason=INPUT_DROPPED)
        if len(data) < _BULK_SUBMIT_THRESHOLD_CHARS:
            # Short sends stay instant (sendSubmit's own rule) — and so stay
            # unverified: there is no settle window to observe an echo in.
            if not submit:
                return InputOutcome(reason=INPUT_UNVERIFIED)
            if not self._write_locked("\r"):
                return InputOutcome(reason=INPUT_DROPPED)
            return InputOutcome(reason=INPUT_UNVERIFIED, submitted=True)
        needles = _echo_needles(data)
        floor_at = sent_at + _BULK_FLOOR_MS / 1000
        settle_deadline = sent_at + _BULK_CAP_MS / 1000
        ingest_deadline = sent_at + _INGEST_CAP_MS / 1000
        quiet_s = _BULK_QUIET_MS / 1000
        ingested = False
        settled = False
        exited = False
        # Only rescan when the terminal has actually painted something new —
        # this loop polls 20×/s on a process hosting every live PTY, and in
        # the case that matters most (nothing coming back at all) there is
        # nothing new to normalize on any of those passes.
        scanned_at = mark
        while True:
            if self._exited:
                exited = True
                break
            if not ingested and self._output_total > scanned_at:
                scanned_at = self._output_total
                ingested = self._echo_seen_since(mark, needles)
            now = time.time()
            settled = (
                now >= floor_at
                and self._last_output_at > sent_at
                and (now - self._last_output_at) >= quiet_s
            )
            if ingested and (settled or now >= settle_deadline):
                break
            # Not ingested yet: keep looking past the settle cap — under load
            # the echo itself is deferred (terminal-compose.js's #499 note) —
            # but never past the ingest cap.
            if now >= ingest_deadline:
                break
            time.sleep(_BULK_POLL_S)
        waited_ms = int((time.time() - sent_at) * 1000)
        if exited:
            return InputOutcome(
                reason=INPUT_DROPPED, ingested=ingested, waited_ms=waited_ms
            )
        if not ingested:
            # The write returned without raising, yet the terminal never
            # painted the payload: treat it as undelivered and withhold the
            # CR rather than firing it into an unknown TUI state.
            return InputOutcome(
                reason=INPUT_NOT_INGESTED, ingested=False, waited_ms=waited_ms
            )
        if not submit:
            return InputOutcome(reason=INPUT_OK, ingested=True, waited_ms=waited_ms)
        if not settled:
            # Ingested, but the stream never went quiet: the agent is working
            # and repainting. Before #763 the CR went out here anyway, into a
            # terminal mid-repaint — exactly the window Claude Code absorbs a
            # CR as a literal newline in, which is how three steers ended up
            # stranded on 2026-08-14. Now nothing is written: the submit is
            # handed to a watcher that presses Enter when the agent actually
            # settles, the way a human at the keyboard would.
            self._defer_args = (self._defer_seq, mark, needles)
            return InputOutcome(
                reason=INPUT_DEFERRED,
                ingested=True,
                deferred=True,
                waited_ms=waited_ms,
            )
        if not self._write_locked("\r"):
            return InputOutcome(
                reason=INPUT_DROPPED, ingested=True, waited_ms=waited_ms
            )
        return InputOutcome(
            reason=INPUT_OK,
            ingested=True,
            submitted=True,
            submit_confirmed=True,
            waited_ms=waited_ms,
        )

    # ------------------------------------------------- deferred submit (#763)
    def _arm_deferred_submit(
        self, seq: int, mark: int, needles: List[str], nbytes: int
    ) -> None:
        """Hand a still-unsubmitted payload to a background watcher thread.

        Daemon so a session-host shutdown is never held up by a watcher
        sitting out its window.
        """
        threading.Thread(
            target=self._run_deferred_submit,
            args=(seq, mark, needles, nbytes),
            name=f"defer-submit-{self.session_id[:8]}",
            daemon=True,
        ).start()

    def _run_deferred_submit(
        self, seq: int, mark: int, needles: List[str], nbytes: int
    ) -> None:
        """Thread body: run the watcher, then record whatever it concluded."""
        try:
            outcome = self._await_deferred_submit(seq, mark, needles)
        except Exception as exc:  # noqa: BLE001 — a watcher must never crash the host
            logger.debug(
                f"PTY {self.session_id[:8]} deferred submit watcher failed: {exc}"
            )
            return
        if outcome is None:
            return  # superseded — ``last_input`` belongs to the newer write
        self._record_input(outcome, nbytes, True)

    def _await_deferred_submit(
        self, seq: int, mark: int, needles: List[str]
    ) -> Optional["InputOutcome"]:
        """Wait for a genuine quiet window, then press Enter — or don't.

        This is the API's version of what a human does with a stranded
        ``[Pasted text #N]`` chip: wait until the agent stops working, check
        the message is still sitting there unsent, and press Enter once. It
        never resends the text (see ``_DEFER_QUIET_MS``'s note).

        Three things have to hold before the CR is written, and any of them
        failing means *nothing* is written — the steer stays stranded and
        honestly reported, which is exactly the pre-#763 state, never worse:

        1. **A real quiet window** (``_DEFER_QUIET_MS``), inside
           ``_DEFER_CAP_MS``. A working agent repaints far more often than
           that, so quiet means its turn is genuinely over.
        2. **The payload is still visible in the terminal's recent output.**
           The composer is repainted on essentially every frame, so a payload
           still sitting unsent keeps reappearing; one that was submitted, or
           scrolled behind something else, stops being repainted. Scanning
           only from the oldest sample in the rolling ``frames`` window
           (~``_DEFER_FRAME_LOOKBACK_MS``) is what makes this "it is there
           now" rather than "it was there once".
        3. **No dialog in that same window** (``_DEFER_DIALOG_MARKERS``) —
           a bare CR into a permission or AskUserQuestion modal picks an
           option instead of submitting.

        Returns ``None`` when a newer write superseded this watcher; the
        caller then leaves ``last_input`` alone, since it now describes that
        newer write.
        """
        started_at = time.time()
        deadline = started_at + _DEFER_CAP_MS / 1000
        quiet_s = _DEFER_QUIET_MS / 1000
        # Seeded with the pre-write mark so an agent that settles almost
        # immediately still matches against its own original echo; the seed
        # ages out of the deque within _DEFER_FRAME_LOOKBACK_MS, after which
        # only fresh repaints count.
        frames: "deque[int]" = deque([mark], maxlen=_DEFER_FRAME_SAMPLES)
        while True:
            if self._defer_seq != seq:
                return None
            now = time.time()
            if self._exited:
                return InputOutcome(
                    reason=INPUT_DROPPED,
                    ingested=True,
                    deferred=True,
                    waited_ms=int((now - started_at) * 1000),
                )
            last_out = self._last_output_at
            if last_out and (now - last_out) >= quiet_s:
                break
            if now >= deadline:
                return InputOutcome(
                    reason=INPUT_DEFER_TIMEOUT,
                    ingested=True,
                    deferred=True,
                    submit_confirmed=False,
                    waited_ms=int((now - started_at) * 1000),
                )
            frames.append(self._output_total)
            time.sleep(_DEFER_POLL_S)

        # Quiet. Re-check under ``_write_lock`` so nothing can write between
        # the check and the CR, and re-check ``_defer_seq`` inside it too —
        # a superseding write bumps the counter while holding the same lock.
        with self._write_lock:
            if self._defer_seq != seq:
                return None
            waited_ms = int((time.time() - started_at) * 1000)
            if self._exited:
                return InputOutcome(
                    reason=INPUT_DROPPED,
                    ingested=True,
                    deferred=True,
                    waited_ms=waited_ms,
                )
            normalized = self._normalized_since(frames[0])
            if not self._payload_visible(normalized, needles):
                return InputOutcome(
                    reason=INPUT_DEFER_VANISHED,
                    ingested=True,
                    deferred=True,
                    submit_confirmed=False,
                    waited_ms=waited_ms,
                )
            if any(marker in normalized for marker in _DEFER_DIALOG_MARKERS):
                return InputOutcome(
                    reason=INPUT_DEFER_UNCLEAR,
                    ingested=True,
                    deferred=True,
                    submit_confirmed=False,
                    waited_ms=waited_ms,
                )
            if not self._write_locked("\r"):
                return InputOutcome(
                    reason=INPUT_DROPPED,
                    ingested=True,
                    deferred=True,
                    waited_ms=waited_ms,
                )
        return InputOutcome(
            reason=INPUT_OK,
            ingested=True,
            submitted=True,
            submit_confirmed=True,
            deferred=True,
            waited_ms=waited_ms,
        )

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
