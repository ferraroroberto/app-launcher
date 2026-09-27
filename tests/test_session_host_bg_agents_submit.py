"""A deferred submit lands while background agents run — issue #1319.

When Claude Code's main agent ends its turn with background sub-agents still
working, it paints "✻ Waiting for N background agents to finish" once and then
ticks a timer on each agent's row, about once a second each, for as long as
they run. The main agent is idle at its prompt, and a CR typed into that
terminal submits, but the stream never has the deferred watcher's 1.5 s quiet
window, so a steer or a Chat message waited until the agents finished or the
watcher gave up with ``defer_timeout``.

These tests drive the session's *real* reader thread with a scripted PTY, so
the turn state is read off the stream the same way the session-host reads it.
The frames are shaped on the ones captured from live sessions: the
differential renderer's cursor-skipped wait line, timer-digit deltas, window
title updates and a main-turn spinner.
"""

from __future__ import annotations

import queue
import threading
import time as real_time

import pytest

from src import session_host_input
from src.session_host_input import (
    INPUT_DEFER_TIMEOUT,
    INPUT_DEFERRED,
    INPUT_OK,
    INPUT_UNVERIFIED,
)
from src.session_host import PtySession
from tests.test_session_host_submit_input import _make_session, clock  # noqa: F401

# Captured before the ``clock`` fixture swaps time.sleep for its fake: the
# harness below needs a real wait for the reader thread to catch up.
_REAL_SLEEP = real_time.sleep
_REAL_MONOTONIC = real_time.monotonic

# The wait line as a live session painted it, with the renderer skipping the
# cells that already held the right character ("backgrou" + "d", "fini" + "h").
_BG_WAIT_FRAME = (
    "\x1b[?25l\x1b[2D\x1b[9B\n\x1b[12A\x1b[38;2;153;153;153m✻\x1b[3GWaiting for "
    "\x1b[1m2\x1b[22m backgrou\x1b[26Gd\x1b[28Gagents to\x1b[38Gfini\x1b[43Gh"
    "\x1b[39m\x1b[K\n\x1b[3B❯\xa0\n\n\x1b[2C\x1b[9A\x1b[?25h"
)
# One agent row's timer ticking over: all the terminal paints while idle.
_TIMER_TICK_FRAME = (
    "\x1b[?25l\x1b[2D\x1b[9B\n\x1b[79C\x1b[2A\x1b[38;2;153;153;153m{digit}\n"
    "\x1b[79C\x1b[1B{digit}\x1b[39m\n\n\x1b[2C\x1b[9A\x1b[?25h"
)
_TITLE_FRAME = "\x1b]0;◐ background task\x1b\\"
# A main turn's spinner: the first frame of a turn, then a glyph-only repaint.
_TURN_START_FRAME = (
    "\x1b[?25l\x1b[2D\x1b[10B\r\x1b[16A\x1b[38;2;215;119;87m✢\x1b[39m "
    "\x1b[38;2;215;119;87mPondering… \x1b[38;2;153;153;153m(0s)\x1b[39m\x1b[K"
    "\x1b[2C\x1b[10A\x1b[?25h"
)
_SPINNER_FRAME = (
    "\x1b[?25l\x1b[2D\x1b[10B\r\x1b[16A\x1b[38;2;215;119;87m✶\x1b[39m\r\r\n"
    "\x1b[2C\x1b[10A\x1b[?25h"
)

_PAYLOAD = "CHIEF - next item in the queue, start it when you can. " * 12
# A few agent rows ticking about once a second each, plus the spinning title,
# as on the live session that stranded a send: denser than the 350 ms paste
# settle, so a bulk send is deferred rather than submitted inline.
_TICK_S = 0.25
# Short enough that a watcher on the old rule reaches its give-up quickly.
_TEST_DEFER_CAP_MS = 60_000


class _ScriptedPty:
    """Feeds scripted chunks to the session's own reader thread.

    ``read`` blocks on a queue the test fills; ``paint`` waits (in real time)
    until the reader has counted the chunk into ``_output_total``, so the
    test and the watcher never race the thread they are observing.
    """

    def __init__(self, session: PtySession) -> None:
        self._session = session
        self._queue: "queue.Queue" = queue.Queue()
        self.writes: list = []
        session._pty.read.side_effect = self._read
        session._pty.isalive.return_value = True
        session._pty.write.side_effect = self.writes.append
        self._thread = threading.Thread(target=session._read_loop, daemon=True)
        self._thread.start()

    def _read(self, _size: int) -> str:
        item = self._queue.get()
        if item is None:
            raise EOFError
        return item

    def paint(self, chunk: str) -> None:
        target = self._session._output_total + len(chunk)
        self._queue.put(chunk)
        deadline = _REAL_MONOTONIC() + 5
        while self._session._output_total < target:
            assert _REAL_MONOTONIC() < deadline, "reader thread never took the chunk"
            _REAL_SLEEP(0.0005)

    def close(self) -> None:
        self._queue.put(None)
        self._thread.join(timeout=5)

    @property
    def crs(self) -> int:
        return self.writes.count("\r")


@pytest.fixture
def scripted(clock, monkeypatch):  # noqa: F811 — the shared fake-clock fixture
    monkeypatch.setattr(session_host_input, "_DEFER_CAP_MS", _TEST_DEFER_CAP_MS)
    session = _make_session()
    session._bracketed_paste_mode = True
    pty = _ScriptedPty(session)
    yield session, pty, clock
    pty.close()


def _defer(session: PtySession, pty: _ScriptedPty, clock, monkeypatch, on_tick) -> tuple:
    """Send ``_PAYLOAD`` through ``submit_input`` and return the watcher's args.

    The composer echoes the paste on the first poll; after that ``on_tick``
    paints whatever the screen is doing every ``_TICK_S``, so the stream is
    never quiet for either the paste-settle or the watcher's window.
    """
    armed: list = []
    monkeypatch.setattr(
        PtySession, "_arm_deferred_submit", lambda self, *a, **kw: armed.append((a, kw))
    )
    state = {"echoed": False, "last_tick": clock.now, "n": 0}

    def _on_sleep(c) -> None:
        if not state["echoed"]:
            state["echoed"] = True
            pty.paint("\x1b[2K❯ " + _PAYLOAD)
        if c.now - state["last_tick"] >= _TICK_S:
            state["last_tick"] = c.now
            state["n"] += 1
            on_tick(state["n"])

    clock.on_sleep(_on_sleep)
    outcome = session.submit_input(_PAYLOAD, True)
    assert outcome.reason == INPUT_DEFERRED
    assert pty.crs == 0
    assert len(armed) == 1
    return armed[0]


def _idle_with_agents(pty: _ScriptedPty):
    """Every tick: an agent row's timer moves on, and the title spins."""

    def _tick(n: int) -> None:
        pty.paint(_TIMER_TICK_FRAME.format(digit=n % 10))
        pty.paint(_TITLE_FRAME)

    return _tick


def test_idle_prompt_with_background_agents_submits_and_confirms(scripted, monkeypatch):
    """#1319's headline: the send lands within seconds and reads ``ok``.

    On the old quiet-window rule this waited out the whole window and
    reported ``defer_timeout`` with nothing written.
    """
    session, pty, clock = scripted
    pty.paint(_BG_WAIT_FRAME)
    idle_tick = _idle_with_agents(pty)

    def _tick(n: int) -> None:
        # Once our CR is in, Claude Code starts the main turn.
        if pty.crs:
            pty.paint(_TURN_START_FRAME)
        idle_tick(n)

    args, kw = _defer(session, pty, clock, monkeypatch, _tick)
    session._run_deferred_submit(*args, **kw)

    assert pty.crs == 1
    assert session.last_input["reason"] == INPUT_OK
    assert session.last_input["submit_state"] == "confirmed"
    assert session.last_input["delivered"] is True
    assert session.last_input["waited_ms"] < 5_000
    # Only the bare CR went in after the paste — never a resend of the text.
    assert pty.writes[-1] == "\r"
    assert "".join(pty.writes[:-1]) == "\x1b[200~" + _PAYLOAD + "\x1b[201~"


def test_a_running_main_turn_still_defers(scripted, monkeypatch):
    """The busy-agent protection #763 exists for is untouched: with the main
    turn's spinner repainting, background agents or not, no CR goes in."""
    session, pty, clock = scripted
    pty.paint(_BG_WAIT_FRAME)
    idle_tick = _idle_with_agents(pty)

    def _tick(n: int) -> None:
        pty.paint(_TURN_START_FRAME if n == 1 else _SPINNER_FRAME)
        idle_tick(n)

    args, kw = _defer(session, pty, clock, monkeypatch, _tick)
    session._run_deferred_submit(*args, **kw)

    assert pty.crs == 0
    assert session.last_input["reason"] == INPUT_DEFER_TIMEOUT
    assert session.last_input["submit_state"] == "not_submitted"


def test_a_submit_no_turn_follows_is_reported_unconfirmed(scripted, monkeypatch):
    """Reading the screen can be wrong, so the CR is checked afterwards: no
    main turn starting means the outcome says unconfirmed, never ``ok``."""
    session, pty, clock = scripted
    pty.paint(_BG_WAIT_FRAME)

    args, kw = _defer(session, pty, clock, monkeypatch, _idle_with_agents(pty))
    session._run_deferred_submit(*args, **kw)

    assert pty.crs == 1
    assert session.last_input["reason"] == INPUT_UNVERIFIED
    assert session.last_input["submitted"] is True
    assert session.last_input["submit_confirmed"] is None
    assert session.last_input["submit_state"] == "unconfirmed"


def test_a_newer_write_during_confirmation_keeps_its_verdict(scripted, monkeypatch):
    """The confirmation wait is long enough for another send to land in it.
    That newer write owns ``last_input`` from then on, so the watcher must
    not overwrite it with its own late verdict — the rule everywhere else in
    the watcher."""
    session, pty, clock = scripted
    pty.paint(_BG_WAIT_FRAME)
    idle_tick = _idle_with_agents(pty)
    newer = {"reason": "newer-write"}

    def _tick(n: int) -> None:
        if pty.crs and session.last_input is not newer:
            # What submit_input does for the newer call, under the same lock.
            session._defer_seq += 1
            session.last_input = newer
        idle_tick(n)

    args, kw = _defer(session, pty, clock, monkeypatch, _tick)
    session._run_deferred_submit(*args, **kw)

    assert pty.crs == 1
    assert session.last_input is newer


def test_the_turn_state_follows_the_stream(scripted):
    session, pty, _clock = scripted
    assert session._bg_waiting is False
    pty.paint(_BG_WAIT_FRAME)
    assert session._bg_waiting is True
    pty.paint(_TIMER_TICK_FRAME.format(digit=3))
    pty.paint(_TITLE_FRAME)
    assert session._bg_waiting is True
    before = session._output_total
    pty.paint(_TURN_START_FRAME)
    assert session._bg_waiting is False
    assert session._main_turn_at > before


@pytest.mark.parametrize(
    "chunk, expected",
    [
        pytest.param(_BG_WAIT_FRAME, True, id="wait-line-cursor-skipped"),
        pytest.param(
            "✻\x1b[3GWaiting for \x1b[1m3\x1b[22m background agents\x1b[35Gto finish",
            True,
            id="wait-line-whole",
        ),
        pytest.param(
            "✻\x1b[3GWaiting for \x1b[1m1\x1b[22m background agent\x1b[34Gto\x1b[37Gfinish",
            True,
            id="wait-line-singular",
        ),
        pytest.param(
            "Waiting for \x1b[1m2\x1b[22m backg\x1b[23Gou\x1b[26Gd agents\x1b[35Gt",
            True,
            id="wait-line-glyph-skipped",
        ),
        pytest.param(_TURN_START_FRAME, False, id="turn-start"),
        pytest.param(_SPINNER_FRAME, False, id="spinner-glyph-only"),
        pytest.param("\x1b[16A✻\x1b[14Gi\x1b[17G…", False, id="spinner-shimmer"),
        pytest.param(
            "✻ Worked for 12s\r\n" + _BG_WAIT_FRAME, True, id="turn-summary-then-wait"
        ),
        pytest.param(_BG_WAIT_FRAME + _TURN_START_FRAME, False, id="wait-then-turn"),
        pytest.param(_TIMER_TICK_FRAME.format(digit=7), None, id="timer-tick"),
        pytest.param("\x1b]0;✳ idle title\x1b\\", None, id="title-osc"),
        pytest.param("\x1b]0;✳ idle ti", None, id="title-osc-cut-off"),
        pytest.param("Waiting for the gate to finish:", None, id="prose-waiting"),
        pytest.param("Waiting for 2 minutes then retrying", None, id="prose-count"),
        pytest.param("· thinking) * 42", None, id="common-text-glyphs"),
    ],
)
def test_turn_state_markers(chunk, expected):
    assert session_host_input._scan_turn_state(chunk) is expected
