"""Scrollback-ring truncation hygiene (issue #444).

A saturated ring used to be cut at an arbitrary character
(``self._ring[-_RING_MAX_CHARS:]``), so a reconnect replay could start
mid-escape-sequence and render the tail of a CSI/OSC as literal garbage at
the top of the replayed scrollback. ``_trim_ring_head`` advances the cut to
the next newline boundary (bounded scan) so the replay head is always a
whole line.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from src.session_host import (
    _RING_MAX_CHARS,
    _RING_TRIM_SCAN,
    PtySession,
    _trim_ring_head,
)


def test_trim_ring_head_skips_partial_first_line():
    # A hard cut landed inside an SGR sequence — the leftover "9;41m…"
    # would render as literal text. The trim resumes at the next line.
    ring = "9;41m tail of a cut escape\nSECOND LINE\nTHIRD LINE\n"
    assert _trim_ring_head(ring) == "SECOND LINE\nTHIRD LINE\n"


def test_trim_ring_head_keeps_raw_cut_when_no_newline_in_window():
    # One enormous unbroken line: better an approximate head than
    # discarding more history than the cap already did.
    ring = "x" * (_RING_TRIM_SCAN + 100) + "\nrest\n"
    assert _trim_ring_head(ring) == ring


class _OneShotPty:
    """A fake PTY that yields one chunk, then EOFs — enough to drive
    ``_read_loop`` synchronously without a background thread."""

    def __init__(self, chunk: str) -> None:
        self._chunk = chunk
        self._sent = False

    def read(self, _n):
        if not self._sent:
            self._sent = True
            return self._chunk
        raise EOFError

    def isalive(self):
        return False


def test_read_loop_overflow_trims_ring_to_line_boundary():
    """Overflowing the ring must leave it starting on a whole line, not the
    tail of whichever line (or escape sequence) the hard cap cut through."""
    line = "line-000000\n"  # 12 chars; _RING_MAX_CHARS % 12 != 0 → mid-line cut
    assert _RING_MAX_CHARS % len(line) != 0
    chunk = line * (_RING_MAX_CHARS // len(line) + 10)
    session = PtySession(
        session_id="sid-test",
        project_dir=r"C:\stub",
        name="proj",
        flags="",
        started_at=time.time(),
        _loop=MagicMock(),
        _pty=_OneShotPty(chunk),
        agent="claude",
    )
    session._read_loop()
    assert len(session._ring) <= _RING_MAX_CHARS
    assert session._ring.startswith("line-"), (
        f"ring head is a partial line: {session._ring[:20]!r} — the replay "
        "would start mid-line/mid-escape"
    )
