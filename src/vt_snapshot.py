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

Thread-safety: ``feed``/``resize`` are called from the PTY reader thread;
``render`` is called from the asyncio event loop when a client subscribes.
A single lock serializes all three against pyte's mutable screen state.
"""

from __future__ import annotations

import threading
from typing import Optional

import pyte
from wcwidth import wcswidth

# Inverse of pyte's ANSI color tables (code -> name) so we can go the other
# way: named color -> SGR code. Any fg/bg pyte hands back that isn't one of
# these eight base names is a 6-hex-digit truecolor/256-palette string (see
# pyte.graphics.FG_BG_256) — rendered via the 38;2/48;2 truecolor SGR form.
_FG_CODE = {name: code for code, name in pyte.graphics.FG_ANSI.items() if name != "default"}
_BG_CODE = {name: code for code, name in pyte.graphics.BG_ANSI.items() if name != "default"}


class VtSnapshot:
    """Thread-safe headless VT screen mirroring one PTY session's output."""

    def __init__(self, rows: int, cols: int) -> None:
        self._lock = threading.Lock()
        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.Stream(self._screen)

    def feed(self, chunk: str) -> None:
        with self._lock:
            self._stream.feed(chunk)

    def resize(self, rows: int, cols: int) -> None:
        with self._lock:
            self._screen.resize(rows, cols)

    def render(self) -> str:
        """Render the current frame as ANSI text: SGR runs + cursor state.

        A cell-by-cell SGR-run encoding (reset-then-set on every attribute
        change) rather than incremental toggling — simpler and cheap at
        terminal-frame scale (tens of KB at most, vs. the hundreds of KB a
        long transcript re-emission costs).

        Each row starts with an absolute cursor-position escape rather than
        relying on natural line wrap / ``\\r\\n``: a row that fills every
        column leaves the cursor in xterm's "deferred autowrap" state, where
        an explicit CR+LF right after the last column produces an extra line
        feed (proven empirically — see the vt_snapshot round-trip probe in
        issue #432 — a naive ``\\r\\n`` join shifted the whole frame down by
        one line). Absolute positioning sidesteps deferred-wrap entirely.
        """
        with self._lock:
            lines = list(self._screen.display)
            buffer = self._screen.buffer
            cols = self._screen.columns
            cursor = self._screen.cursor
            cursor_y, cursor_x, cursor_hidden = cursor.y, cursor.x, cursor.hidden

        out: list[str] = []
        for y in range(len(lines)):
            out.append(f"\x1b[{y + 1};1H")
            prev_attrs: Optional[tuple] = None
            row = buffer[y]
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
                # A wide (e.g. emoji) cell's other half is a blank
                # continuation slot in pyte's buffer — the receiving
                # terminal's own wcwidth-aware cursor advance already skips
                # it once we've emitted the wide char, so we must not also
                # write to it (that shifted everything after it by one
                # column — caught by the vt_snapshot round-trip probe).
                width = wcswidth(data)
                x += width if width and width > 0 else 1
        out.append("\x1b[0m")
        out.append(f"\x1b[{cursor_y + 1};{cursor_x + 1}H")
        out.append("\x1b[?25l" if cursor_hidden else "\x1b[?25h")
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
