"""Headless VT100 screen mirror for full-screen (ratatui) PTY sessions.

A ratatui-based agent (Codex/Antigravity/Pi/Copilot, see :mod:`src.agents`)
re-emits its **entire transcript** on any winsize change (empirical probe,
issue #430: ~65 KB for a long conversation, on a same-shape resize toggle).
The session-host used to serve a (re)connecting client by toggling the PTY
width by one column and back (``_force_repaint`` in
``app/session_host/server.py``) so the agent would repaint at the current
size — but that SIGWINCH is exactly what triggers the full re-emission,
visible to every subscriber (issue #432).

:class:`VtSnapshot` avoids the SIGWINCH entirely: it parses the PTY's byte
stream through a headless ``pyte`` screen (fed from the same reader thread
that already fills the raw scrollback ring — see
:class:`src.session_host.PtySession`), so it always mirrors the *current*
frame. On a WS (re)connect the server renders that screen straight to ANSI
(SGR runs + cursor position) and sends it as the opening frame — no resize,
no agent re-emission, no reconnect flash.

``render()`` also prepends a bounded window of real scrollback (issue #435):
a plain :class:`pyte.Screen` only ever knows the current frame, so a client
reconnecting cold (no prior xterm state of its own) landed on exactly one
screen with nothing to scroll into. :class:`pyte.HistoryScreen` tracks lines
that scroll off the top in ``screen.history.top`` — rendered as ordinary
scrolling text (not absolute-positioned) so the client's own terminal
emulator accumulates it into its own scrollback the way live output would,
*before* the current frame paints over the viewport.

Thread-safety: ``feed``/``resize`` are called from the PTY reader thread;
``render`` is called from the asyncio event loop when a client subscribes.
A single lock serializes all three against pyte's mutable screen state.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import pyte
from wcwidth import wcswidth

logger = logging.getLogger(__name__)

# Inverse of pyte's ANSI color tables (code -> name) so we can go the other
# way: named color -> SGR code. Any fg/bg pyte hands back that isn't one of
# these eight base names is a 6-hex-digit truecolor/256-palette string (see
# pyte.graphics.FG_BG_256) — rendered via the 38;2/48;2 truecolor SGR form.
_FG_CODE = {name: code for code, name in pyte.graphics.FG_ANSI.items() if name != "default"}
_BG_CODE = {name: code for code, name in pyte.graphics.BG_ANSI.items() if name != "default"}

# Bounded scrollback window (issue #435): enough to page back through a
# genuine chunk of conversation without the reconnect payload ballooning
# back toward the full-transcript re-emission #430/#432 exist to avoid.
# Twice undersized in practice, each time re-measured against a real live
# session rather than guessed: 500 was cleared by a single README dump
# (797 lines); 2000 was cleared TWICE by one heavy Codex exchange (a user
# question followed by tool-heavy investigation) that produced 3371 total
# lines — and because the user's own message always sits chronologically
# *before* the agent's much longer response within an exchange, FIFO
# eviction always drops the user's side first, which is exactly the
# "my question disappeared, the answer didn't" pattern reported live.
# That whole 3371-line real session still rendered to only ~216 KB — a
# reasonable one-time reconnect payload (zero-cost to the agent; pure
# server-side memory replay, no SIGWINCH). 10,000 gives ~3x headroom over
# the heaviest real sample seen so far; at the same ~65 bytes/line observed
# rate that's a worst-case ~650 KB payload, still well within what the
# existing repaint-batch concealment handles.
#
# User-configurable (issue #435 follow-up): the Settings tab exposes this
# as `terminal_history_lines` (src/webapp_config.py), threaded through
# SessionManager.create()'s `history_lines` param. This constant is only
# the fallback for a caller that doesn't pass one (e.g. a direct
# VtSnapshot(rows, cols) in a test).
_HISTORY_LINES = 10_000

# Minimum gap between "pyte parse error" warning breadcrumbs (issue #711):
# a stuck agent could otherwise emit one every chunk and flood the log.
_FEED_ERROR_LOG_INTERVAL = 30.0


class _MutedQueryScreen(pyte.HistoryScreen):
    """A ``pyte.HistoryScreen`` that never answers terminal queries.

    :class:`VtSnapshot` is a passive mirror with no reply channel — the same
    principle behind the session-host's OSC 10/11/12 colour-reply stripping
    (``_strip_color_osc``, issue #270). ``report_device_status`` is the CSI
    analogue: pyte dispatches *any* private CSI as
    ``csi_dispatch[char](*params, private=True)`` (``pyte/streams.py``), but
    upstream ``Screen.report_device_status(self, mode)`` doesn't accept that
    keyword and raises ``TypeError``. A real terminal answers a private DSR
    (e.g. Copilot CLI 1.0.77's ``ESC[?996n`` light/dark colour-scheme query)
    over its PTY input; this mirror has no such channel, so the only correct
    behaviour is to no-op rather than crash or answer. Non-private DSRs
    (``mode`` without ``private=True``) still delegate to the real
    implementation, matching pyte's own default behaviour.
    """

    def report_device_status(self, mode: int = 0, **kwargs) -> None:
        if kwargs.get("private"):
            return
        super().report_device_status(mode)


class VtSnapshot:
    """Thread-safe headless VT screen mirroring one PTY session's output."""

    def __init__(self, rows: int, cols: int, history: int = _HISTORY_LINES) -> None:
        self._lock = threading.Lock()
        self._screen = _MutedQueryScreen(cols, rows, history=history)
        self._stream = pyte.Stream(self._screen)
        self._last_feed_error_at = 0.0

    def feed(self, chunk: str) -> None:
        """Feed one chunk of raw PTY output into the mirror.

        Any exception pyte's parser raises here is swallowed rather than
        propagated (issue #711): this runs on the PTY reader thread's hot
        path (``session_host.py::PtySession._read_loop``), whose own
        ``try`` covers only the ``read()`` call — an uncaught exception here
        used to kill that thread silently, leaving the session reporting
        alive (input still worked) while scrollback, transcript, and every
        subscriber went permanently dark. Safe to degrade-and-continue:
        pyte reinitialises its parser coroutine whenever a handler raises
        (upstream PR #101), so the *next* chunk parses from a clean state —
        only the chunk that triggered the error is incompletely mirrored.
        """
        with self._lock:
            try:
                self._stream.feed(chunk)
            except Exception:
                now = time.monotonic()
                if now - self._last_feed_error_at >= _FEED_ERROR_LOG_INTERVAL:
                    self._last_feed_error_at = now
                    logger.warning(
                        "VtSnapshot.feed: pyte parser error, chunk dropped "
                        "from mirror (reader thread continues)",
                        exc_info=True,
                    )

    def resize(self, rows: int, cols: int) -> None:
        with self._lock:
            self._screen.resize(rows, cols)

    def render(self) -> str:
        """Render the reconnect payload: bounded scrollback history (#435)
        as plain scrolling text, followed by the current frame as ANSI
        (SGR runs + cursor state).

        History lines are terminated with real ``\\r\\n`` — ordinary
        scrolling text, not absolute-positioned — so the client's own
        xterm accumulates them into its own scrollback exactly the way
        live output would. The current frame is then painted with an
        absolute cursor-position escape per row rather than relying on
        natural line wrap: a row that fills every column leaves the
        cursor in xterm's "deferred autowrap" state, where an explicit
        CR+LF right after the last column produces an extra line feed
        (proven empirically — see the vt_snapshot round-trip probe in
        issue #432 — a naive ``\\r\\n`` join shifted the whole frame down
        by one line). Absolute positioning sidesteps deferred-wrap
        entirely and lands the frame on the live state regardless of how
        much history text scrolled before it.
        """
        with self._lock:
            history_rows = list(self._screen.history.top)
            cols = self._screen.columns
            rows = self._screen.lines
            frame = self._render_frame_locked()
        history_text = "".join(
            _render_row(row, cols) + "\r\n" for row in history_rows
        )
        # Force the viewport clear via real linefeeds before the frame's
        # absolute-positioned paint (issue #435 follow-up: "conversation
        # beginning visible, a chunk in the middle missing, latest lines
        # visible"). After the history text scrolls naturally, the client's
        # viewport still holds the LAST `rows` history lines — only content
        # that scrolls OFF the viewport via a real linefeed becomes
        # scrollback. The frame then addresses the viewport with absolute
        # positions (``\x1b[1;1H`` etc.), which overwrites whatever is
        # still sitting there IN PLACE rather than scrolling it off first —
        # silently erasing exactly that seam's worth of history, proven via
        # a viewport-accurate reparse in the dev-loop probe for this issue.
        # `rows` blank linefeeds guarantee every one of those lines gets a
        # real scroll (and therefore a scrollback entry) before the frame
        # ever touches the viewport. Skipped entirely when there's no
        # history to protect — nothing has scrolled off yet, so there's
        # nothing sitting in the viewport for the frame to clobber, and
        # unconditionally forcing it would inject stray blank lines above
        # every frame (the #432 backward-compatible empty-history case).
        force_scroll = ("\r\n" * rows) if history_rows else ""
        return history_text + force_scroll + frame

    def _render_frame_locked(self) -> str:
        """Render the current frame only. Caller must hold ``self._lock``."""
        lines = self._screen.display
        buffer = self._screen.buffer
        cols = self._screen.columns
        cursor = self._screen.cursor

        out: list[str] = []
        for y in range(len(lines)):
            out.append(f"\x1b[{y + 1};1H")
            out.append(_render_row(buffer[y], cols))
        out.append("\x1b[0m")
        out.append(f"\x1b[{cursor.y + 1};{cursor.x + 1}H")
        out.append("\x1b[?25l" if cursor.hidden else "\x1b[?25h")
        return "".join(out)


def _render_row(row, cols: int) -> str:
    """Render one screen/history row as SGR-run-encoded text (no leading
    cursor positioning, no trailing reset — the caller adds those)."""
    out: list[str] = []
    prev_attrs: Optional[tuple] = None
    x = 0
    while x < cols:
        ch = row[x]
        attrs = (
            ch.fg, ch.bg, ch.bold, ch.italics,
            ch.underscore, ch.strikethrough, ch.reverse, ch.blink,
        )
        if attrs != prev_attrs:
            out.append(_sgr_for(attrs))
            prev_attrs = attrs
        data = ch.data or " "
        out.append(data)
        # A wide (e.g. emoji) cell's other half is a blank continuation
        # slot in pyte's buffer — the receiving terminal's own
        # wcwidth-aware cursor advance already skips it once we've emitted
        # the wide char, so we must not also write to it (that shifted
        # everything after it by one column — caught by the vt_snapshot
        # round-trip probe, issue #432).
        width = wcswidth(data)
        x += width if width and width > 0 else 1
    return "".join(out)


def _sgr_for(attrs: tuple) -> str:
    fg, bg, bold, italics, underscore, strikethrough, reverse, blink = attrs
    codes = ["0"]
    if bold:
        codes.append("1")
    if italics:
        codes.append("3")
    if underscore:
        codes.append("4")
    if blink:
        codes.append("5")
    if reverse:
        codes.append("7")
    if strikethrough:
        codes.append("9")
    codes.extend(_color_codes(fg, _FG_CODE, truecolor_prefix="38"))
    codes.extend(_color_codes(bg, _BG_CODE, truecolor_prefix="48"))
    return f"\x1b[{';'.join(codes)}m"


def _color_codes(color: str, named: dict, truecolor_prefix: str) -> list:
    if color == "default":
        return []
    code = named.get(color)
    if code is not None:
        return [str(code)]
    if len(color) == 6:
        try:
            r, g, b = int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
        except ValueError:
            return []
        return [f"{truecolor_prefix};2;{r};{g};{b}"]
    return []
