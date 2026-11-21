"""Screen-first replay of an inline session's scrollback ring.

A terminal (re)connect used to receive the whole raw ring (up to 256 KB) as
one frame, so the first thing a person saw waited on that whole payload
arriving and being parsed. A client that asks for ``order=screen`` instead
gets the ring's tail first, which redraws the current screen on its own,
then a marker frame, then the full replay it would have had before. The
client applies the full replay in one go, so the screen it is already
showing is replaced by the same screen with all its history above it.

The full replay is the old single frame byte for byte, so scrolling back
shows exactly what it always did: nothing is dropped and nothing repeated.
A client that does not ask keeps the single frame, and an older host
ignores the query parameter, so either side can run an older build.
"""

from __future__ import annotations

import json
from typing import List

# Query value a client sends as ``?order=screen`` to get the screen first.
SCREEN_FIRST = "screen"

# Sent between the screen frame and the full replay. JSON like the
# ``shutdown`` frame, so the client can tell it from terminal output.
HISTORY_MARKER = json.dumps({"type": "history"})

# Upper bound on the screen frame. A screen of rendered lines with their SGR
# decoration fits in a few KB, so this leaves room for a tall phone viewport
# while keeping the first frame small next to a full 256 KB ring.
SCREEN_TAIL_MAX_CHARS = 32 * 1024

# Lines of tail per viewport row. Twice the viewport covers the live region
# an inline agent redraws with cursor-up moves, so the tail on its own lands
# on the current screen rather than a half-drawn one.
_TAIL_LINES_PER_ROW = 2


def screen_tail_start(ring: str, rows: int) -> int:
    """Index where the screen tail starts, or 0 when the ring is not split.

    The tail starts right after a newline, so it never opens inside an
    escape sequence. A ring that already fits the tail budget, or has no
    newline inside it, is not split.
    """
    if len(ring) <= SCREEN_TAIL_MAX_CHARS:
        return 0
    floor = len(ring) - SCREEN_TAIL_MAX_CHARS
    start = len(ring)
    for _ in range(max(rows, 1) * _TAIL_LINES_PER_ROW):
        newline = ring.rfind("\n", floor, start)
        if newline < 0:
            break
        start = newline
    return start + 1 if start < len(ring) else 0


def replay_frames(ring: str, rows: int, clear: str) -> List[str]:
    """The frames a screen-first client receives for ``ring``.

    ``[clear + tail, HISTORY_MARKER, clear + ring]`` when the ring is split,
    else the single ``[clear + ring]`` frame every client used to get.
    """
    start = screen_tail_start(ring, rows)
    if start <= 0:
        return [clear + ring]
    return [clear + ring[start:], HISTORY_MARKER, clear + ring]
