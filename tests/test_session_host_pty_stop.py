"""``PtySession.stop`` — graceful-then-force stop model (issue #253).

The single "Stop and kill" button drives ``STOP_QUIT``: type the agent's
own quit command, wait for a clean exit (so its shutdown hooks run), then
force-terminate only as a fallback. Every terminating stop fires the
cooperative ``{"type":"shutdown"}`` WebSocket frame to every live
subscriber (the mirror page listens for it and self-closes). ``STOP_KILL``
force-terminates immediately; ``STOP_INTERRUPT`` is a Ctrl+C that leaves
the session running (and so must NOT signal shutdown).

The PTY itself is mocked (``MagicMock``) — these are pure unit tests of
``PtySession``, no real ConPTY involvement.
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
async def test_stop_quit_types_esc_then_quit_and_exits_cleanly():
    """STOP_QUIT clears the prompt with ESC, types the agent's /quit, and
    when the agent exits within the grace window it must NOT force-kill."""
    loop = asyncio.get_running_loop()
    session = _make_session(loop)
    session._pty.isalive.return_value = False  # exits immediately on /quit

    session.stop(mode=STOP_QUIT)

    calls = [c.args for c in session._pty.write.call_args_list]
    assert ("\x1b",) in calls
    assert ("/quit\r",) in calls
    session._pty.terminate.assert_not_called()


@pytest.mark.asyncio
async def test_stop_quit_uses_per_agent_command():
    """STOP_QUIT types the agent's *own* quit command — Copilot's /exit,
    not Claude's /quit."""
    loop = asyncio.get_running_loop()
    session = _make_session(loop, agent="copilot")
    session._pty.isalive.return_value = False

    session.stop(mode=STOP_QUIT)

    calls = [c.args for c in session._pty.write.call_args_list]
    assert ("\x1b",) in calls
    assert ("/exit\r",) in calls
    assert ("/quit\r",) not in calls
    session._pty.terminate.assert_not_called()


@pytest.mark.asyncio
async def test_stop_quit_force_terminates_when_agent_does_not_exit():
    """If the agent never exits on its quit command within the grace
    window, the fallback force-terminate is the guarantee a stop ends the
    session (issue #253)."""
    loop = asyncio.get_running_loop()
    session = _make_session(loop)
    session._pty.isalive.return_value = True  # never exits on its own

    session.stop(mode=STOP_QUIT, grace_seconds=0.2)

    # Quit was still attempted first…
    calls = [c.args for c in session._pty.write.call_args_list]
    assert ("/quit\r",) in calls
    # …then the fallback force-kill fired.
    session._pty.terminate.assert_called_once_with(force=True)


@pytest.mark.asyncio
async def test_stop_kill_force_terminates_immediately():
    """STOP_KILL skips the graceful step entirely."""
    loop = asyncio.get_running_loop()
    session = _make_session(loop)

    session.stop(mode=STOP_KILL)

    session._pty.terminate.assert_called_once_with(force=True)
    session._pty.write.assert_not_called()


@pytest.mark.asyncio
async def test_stop_interrupt_sends_ctrl_c_and_does_not_signal_shutdown():
    """STOP_INTERRUPT is a Ctrl+C — the session keeps running, so no
    terminate and no mirror shutdown frame."""
    loop = asyncio.get_running_loop()
    session = _make_session(loop)
    _snapshot, queue = session.subscribe()

    session.stop(mode=STOP_INTERRUPT)

    session._pty.sendintr.assert_called_once()
    session._pty.terminate.assert_not_called()
    session._pty.write.assert_not_called()
    await asyncio.sleep(0)
    assert queue.empty()


@pytest.mark.asyncio
async def test_stop_quit_signals_all_subscribers():
    """Every terminating stop self-closes the mirror — both the phone WS
    and the mirror-page WS receive {"type":"shutdown"}."""
    loop = asyncio.get_running_loop()
    session = _make_session(loop)
    session._pty.isalive.return_value = False
    _, q_phone = session.subscribe()
    _, q_mirror = session.subscribe()

    session.stop(mode=STOP_QUIT)

    await asyncio.sleep(0)
    assert json.loads(q_phone.get_nowait()) == {"type": "shutdown"}
    assert json.loads(q_mirror.get_nowait()) == {"type": "shutdown"}


@pytest.mark.asyncio
async def test_stop_kill_signals_subscribers():
    """The immediate-force path closes the window too."""
    loop = asyncio.get_running_loop()
    session = _make_session(loop)
    _snapshot, queue = session.subscribe()

    session.stop(mode=STOP_KILL)

    await asyncio.sleep(0)
    assert json.loads(queue.get_nowait()) == {"type": "shutdown"}


@pytest.mark.asyncio
async def test_stop_after_exit_is_a_noop():
    """Already-exited session — must complete cleanly without raising."""
    loop = asyncio.get_running_loop()
    session = _make_session(loop)
    session._exited = True

    session.stop(mode=STOP_QUIT)
    await asyncio.sleep(0)  # let any scheduled callbacks drain
    # _exited → alive is False, so no force-kill is needed.
    session._pty.terminate.assert_not_called()
