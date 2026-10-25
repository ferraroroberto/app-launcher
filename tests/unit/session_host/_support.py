"""Shared helpers for the session-host input tests: a fake-PTY session, its echo and a controllable clock."""

from __future__ import annotations

import time as real_time
from unittest.mock import MagicMock


from src.session_host import PtySession


def _make_session() -> PtySession:
    pty = MagicMock(name="PtyProcess")
    return PtySession(
        session_id="sid-test",
        project_dir=r"C:\stub",
        name="claude",
        flags="",
        started_at=real_time.time(),
        _loop=MagicMock(),
        _pty=pty,
    )


def _echo(session: PtySession, text: str) -> None:
    """Simulate the reader thread painting ``text`` back out of the PTY.

    The real ingest signal (#760) is what the terminal *paints*, so a test
    that wants a bulk payload to submit has to echo it — same as a live
    Claude Code composer echoing a paste or collapsing it into a chip.
    """
    with session._ring_lock:
        session._output_total += len(text)
        session._ring += text


class _FakeClock:
    """A controllable time.time()/time.sleep() double, module-patched onto
    src.session_host so submit_input's settle wait is deterministic and
    instant instead of racing a real wall clock."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start
        self.sleep_calls: list[float] = []
        self._on_sleep = None  # optional callback(clock) fired each sleep()

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.now += seconds
        if self._on_sleep is not None:
            self._on_sleep(self)

    def on_sleep(self, fn) -> None:
        self._on_sleep = fn
