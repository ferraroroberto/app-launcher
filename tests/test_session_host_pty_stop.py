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

from src.agents import SESSION_HOST_AGENTS, quit_command_for
from src.session_host import (
    _STOP_KEY_SETTLE_SECONDS,
    STOP_INTERRUPT,
    STOP_KILL,
    STOP_QUIT,
    PtySession,
)


def _writes(session: PtySession) -> list:
    """The payloads handed to the PTY, in order, one entry per write."""
    return [c.args[0] for c in session._pty.write.call_args_list]


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

    session.stop(mode=STOP_QUIT, key_settle_seconds=0)

    assert _writes(session) == ["\x1b", "/quit", "\r"]
    session._pty.terminate.assert_not_called()


@pytest.mark.asyncio
async def test_stop_quit_uses_per_agent_command():
    """STOP_QUIT types the agent's *own* quit command — Copilot's /exit,
    not Claude's /quit."""
    loop = asyncio.get_running_loop()
    session = _make_session(loop, agent="copilot")
    session._pty.isalive.return_value = False

    session.stop(mode=STOP_QUIT, key_settle_seconds=0)

    assert _writes(session) == ["\x1b", "/exit", "\r"]
    session._pty.terminate.assert_not_called()


@pytest.mark.asyncio
async def test_stop_quit_force_terminates_when_agent_does_not_exit():
    """If the agent never exits on its quit command within the grace
    window, the fallback force-terminate is the guarantee a stop ends the
    session (issue #253)."""
    loop = asyncio.get_running_loop()
    session = _make_session(loop)
    session._pty.isalive.return_value = True  # never exits on its own

    session.stop(mode=STOP_QUIT, grace_seconds=0.2, key_settle_seconds=0)

    # Quit was still attempted first…
    assert _writes(session) == ["\x1b", "/quit", "\r"]
    # …then the fallback force-kill fired.
    session._pty.terminate.assert_called_once_with(force=True)


@pytest.mark.asyncio
async def test_stop_quit_types_three_separate_keystrokes():
    """#1016 — ESC, the command and the submitting CR are three writes.

    This is the shape the pre-fix code got wrong, and the assertion that
    fails against it: it sent ``"\\x1b"`` then ``"/quit\\r"``, bursting the
    slash onto the ESC (Pi/Antigravity/Copilot read ``ESC /`` as one
    meta-key and dropped the slash) and the CR onto the command (Codex left
    ``/quit`` in an open popup and never exited). Runs at the real default
    settle so the shipped constant is what gets exercised.
    """
    loop = asyncio.get_running_loop()
    session = _make_session(loop)
    session._pty.isalive.return_value = False

    session.stop(mode=STOP_QUIT)

    assert _writes(session) == ["\x1b", "/quit", "\r"]


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", sorted(SESSION_HOST_AGENTS))
async def test_stop_quit_never_bursts_keystrokes_together(agent):
    """#1016 — no single write may carry two of the three keystrokes.

    Covers every agent the session-host accepts, so a harness added later
    inherits the guard instead of quietly re-acquiring the bug. The bytes
    themselves are unchanged from pre-fix: same command, same order, only
    delivered as separate keystrokes.
    """
    loop = asyncio.get_running_loop()
    session = _make_session(loop, agent=agent)
    session._pty.isalive.return_value = False
    command = quit_command_for(agent)

    session.stop(mode=STOP_QUIT, key_settle_seconds=0)

    writes = _writes(session)
    assert writes == ["\x1b", command, "\r"]
    # Restated as the two races, so a future rewrite that still passes the
    # equality above for the wrong reason can't reintroduce either one.
    assert not any("\x1b" in w and command in w for w in writes)
    assert not any(command in w and "\r" in w for w in writes)


@pytest.mark.asyncio
async def test_stop_quit_beats_between_keystrokes():
    """#1016 — the keystrokes are actually spaced, not merely split.

    Splitting the writes is not enough on its own: the ESC→slash race was
    reproduced with two back-to-back writes, and only a real pause fixed
    it. Lower bound only, so a loaded box can't make this flaky.
    """
    loop = asyncio.get_running_loop()
    session = _make_session(loop)
    session._pty.isalive.return_value = False

    started = time.monotonic()
    session.stop(mode=STOP_QUIT, key_settle_seconds=0.05)
    elapsed = time.monotonic() - started

    assert elapsed >= 0.1  # two beats, one after ESC and one before the CR
    # Measured floor: Pi ate the slash at 25 ms and kept it at 50 ms, so the
    # shipped default must not be trimmed below the threshold it clears.
    assert _STOP_KEY_SETTLE_SECONDS >= 0.05


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
