"""``PtySession.stop(close_window=True)`` — cooperative WS shutdown frame.

Issue #20 chose the webapp-side Win32 close (approach C); the
session-host's job in the same stop call is just to fire the
cooperative ``{"type":"shutdown"}`` WebSocket frame to every live
subscriber (from step 7), which the mirror page listens for and
self-closes on. This file pins that behaviour so a future refactor
can't quietly drop it.

The PTY itself is mocked (``MagicMock``) — these are pure unit tests
of ``PtySession``, no real ConPTY involvement.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import MagicMock

import pytest

from src.session_host import (
    STOP_INTERRUPT,
    STOP_KILL,
    STOP_QUIT,
    PtySession,
)


def _make_session(loop, agent: str = "claude") -> PtySession:
    pty = MagicMock(name="PtyProcess")
    return PtySession(
        session_id="sid-test",
        project_dir=r"C:\stub",
        name="claude",
        flags="",
        started_at=time.time(),
        _loop=loop,
        _pty=pty,
        agent=agent,
    )


@pytest.mark.asyncio
async def test_stop_close_window_true_sends_shutdown_frame():
    """Mirror page expects {"type":"shutdown"} on its WS to self-close."""
    loop = asyncio.get_running_loop()
    session = _make_session(loop)
    _snapshot, queue = session.subscribe()

    session.stop(mode=STOP_QUIT, close_window=True)

    # call_soon_threadsafe is async w.r.t. the calling thread; give the
    # loop one tick to drain the scheduled put_nowait.
    await asyncio.sleep(0)
    item = queue.get_nowait()
    assert isinstance(item, str)
    parsed = json.loads(item)
    assert parsed == {"type": "shutdown"}


@pytest.mark.asyncio
async def test_stop_close_window_false_does_not_signal_subscribers():
    """The default (Stop, not Stop & Close) leaves the mirror window alone."""
    loop = asyncio.get_running_loop()
    session = _make_session(loop)
    _snapshot, queue = session.subscribe()

    session.stop(mode=STOP_QUIT, close_window=False)

    await asyncio.sleep(0)
    assert queue.empty()


@pytest.mark.asyncio
async def test_stop_quit_writes_esc_then_slash_quit_to_pty():
    """Default mode is /quit — Claude Code's clean exit."""
    loop = asyncio.get_running_loop()
    session = _make_session(loop)

    session.stop(mode=STOP_QUIT, close_window=False)

    # ESC clears any partial prompt, then "/quit\r" lands cleanly.
    calls = [c.args for c in session._pty.write.call_args_list]
    assert ("\x1b",) in calls
    assert ("/quit\r",) in calls


@pytest.mark.asyncio
async def test_stop_quit_uses_per_agent_command():
    """STOP_QUIT types the agent's *own* quit command — Copilot's /exit,
    not Claude's /quit."""
    loop = asyncio.get_running_loop()
    session = _make_session(loop, agent="copilot")

    session.stop(mode=STOP_QUIT, close_window=False)

    calls = [c.args for c in session._pty.write.call_args_list]
    assert ("\x1b",) in calls
    assert ("/exit\r",) in calls
    assert ("/quit\r",) not in calls


@pytest.mark.asyncio
async def test_stop_close_window_force_terminates_pty():
    """Stop & Close force-kills the ConPTY outright — it must not rely on
    a typed quit command landing (the bug behind a lingering session)."""
    loop = asyncio.get_running_loop()
    session = _make_session(loop, agent="copilot")

    session.stop(mode=STOP_QUIT, close_window=True)

    session._pty.terminate.assert_called_once_with(force=True)
    # No quit command typed — termination is the guarantee.
    session._pty.write.assert_not_called()


@pytest.mark.asyncio
async def test_stop_interrupt_calls_sendintr():
    loop = asyncio.get_running_loop()
    session = _make_session(loop)

    session.stop(mode=STOP_INTERRUPT, close_window=False)

    session._pty.sendintr.assert_called_once()
    session._pty.write.assert_not_called()


@pytest.mark.asyncio
async def test_stop_kill_terminates_pty():
    loop = asyncio.get_running_loop()
    session = _make_session(loop)

    session.stop(mode=STOP_KILL, close_window=False)

    session._pty.terminate.assert_called_once_with(force=True)


@pytest.mark.asyncio
async def test_stop_close_window_signals_all_subscribers():
    """Both the phone WS and the mirror-page WS receive the shutdown frame."""
    loop = asyncio.get_running_loop()
    session = _make_session(loop)
    _, q_phone = session.subscribe()
    _, q_mirror = session.subscribe()

    session.stop(mode=STOP_QUIT, close_window=True)

    await asyncio.sleep(0)
    assert json.loads(q_phone.get_nowait()) == {"type": "shutdown"}
    assert json.loads(q_mirror.get_nowait()) == {"type": "shutdown"}


@pytest.mark.asyncio
async def test_stop_close_window_after_exit_is_a_noop():
    """No subscribers / dead session — must not raise."""
    loop = asyncio.get_running_loop()
    session = _make_session(loop)
    session._exited = True

    # No subscribers, no PTY interactions still required by the contract,
    # but the call must complete cleanly.
    session.stop(mode=STOP_QUIT, close_window=True)
    await asyncio.sleep(0)  # let any scheduled callbacks drain
