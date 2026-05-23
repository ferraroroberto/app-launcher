"""``PtySession.write`` — chunk-and-pace path for long writes (#64).

pywinpty 3.0.3's ``PtyProcess.write()`` wraps an asynchronous ConPTY input
pipe; on long single writes the tail was being silently dropped (#64), and
the previous attempt to compensate via a return-value-driven retry loop
caused massive byte amplification (#13 revert). The fix is to keep small
writes one-shot and split larger payloads into ~512 B chunks with a brief
inter-chunk pause so the pipe drains between writes.

These tests pin the wrapper's behaviour against a fake PTY that records
every call, so a future refactor can't quietly reintroduce either
truncation or the retry-loop regression.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from src.session_host import (
    _WRITE_CHUNK_PAUSE,
    _WRITE_CHUNK_SIZE,
    _WRITE_CHUNK_THRESHOLD,
    PtySession,
)


def _make_session(loop) -> PtySession:
    pty = MagicMock(name="PtyProcess")
    return PtySession(
        session_id="sid-test",
        project_dir=r"C:\stub",
        name="claude",
        flags="",
        started_at=time.time(),
        _loop=loop,
        _pty=pty,
    )


@pytest.mark.asyncio
async def test_short_write_is_one_shot():
    """Writes at or below the threshold go to the PTY in a single call —
    the common case (single keystrokes, short pastes) must not pay the
    chunking cost or change behaviour."""
    loop = asyncio.get_running_loop()
    session = _make_session(loop)

    session.write("hello")

    assert session._pty.write.call_count == 1
    session._pty.write.assert_called_once_with("hello")


@pytest.mark.asyncio
async def test_write_at_threshold_is_one_shot():
    """Boundary: exactly _WRITE_CHUNK_THRESHOLD chars still goes in one shot."""
    loop = asyncio.get_running_loop()
    session = _make_session(loop)
    payload = "x" * _WRITE_CHUNK_THRESHOLD

    session.write(payload)

    assert session._pty.write.call_count == 1
    session._pty.write.assert_called_once_with(payload)


@pytest.mark.asyncio
async def test_long_write_is_chunked_and_concatenates_back_to_input():
    """A multi-KB paste is split into <= _WRITE_CHUNK_SIZE chunks whose
    concatenation equals the input — no bytes added, dropped, or reordered."""
    loop = asyncio.get_running_loop()
    session = _make_session(loop)
    payload = "".join(chr(0x41 + (i % 26)) for i in range(2048))  # 2 KB

    session.write(payload)

    calls = [c.args[0] for c in session._pty.write.call_args_list]
    assert len(calls) > 1, "long write must be chunked"
    assert all(len(c) <= _WRITE_CHUNK_SIZE for c in calls)
    assert "".join(calls) == payload


@pytest.mark.asyncio
async def test_chunked_write_paces_between_chunks():
    """Each inter-chunk gap is at least _WRITE_CHUNK_PAUSE — the pause is
    the whole point of chunk-and-pace, it gives ConPTY's input pipe time
    to drain rather than backpressuring."""
    loop = asyncio.get_running_loop()
    session = _make_session(loop)
    payload = "x" * (_WRITE_CHUNK_SIZE * 4)  # 4 chunks → 3 gaps

    started = time.perf_counter()
    session.write(payload)
    elapsed = time.perf_counter() - started

    # 3 gaps × pause is the theoretical minimum; allow some slack for
    # MagicMock overhead but assert at least ~2 gaps' worth elapsed.
    assert elapsed >= 2 * _WRITE_CHUNK_PAUSE


@pytest.mark.asyncio
async def test_long_write_does_not_retry_on_pty_return_value():
    """Regression guard for #13: pywinpty's PtyProcess.write() can return
    0 for a write that *is* in flight. Interpreting that as "nothing sent,
    retry" amplified a single keystroke into thousands of duplicates. The
    wrapper must ignore the return value and write each chunk exactly once."""
    loop = asyncio.get_running_loop()
    session = _make_session(loop)
    session._pty.write.return_value = 0  # the trap from #13
    payload = "y" * (_WRITE_CHUNK_SIZE * 3)

    session.write(payload)

    # Exactly one write per chunk — no retries.
    assert session._pty.write.call_count == 3
    calls = [c.args[0] for c in session._pty.write.call_args_list]
    assert "".join(calls) == payload


@pytest.mark.asyncio
async def test_write_after_exit_is_noop():
    """A write into a dead session must not touch the (possibly closed) PTY."""
    loop = asyncio.get_running_loop()
    session = _make_session(loop)
    session._exited = True

    session.write("x" * (_WRITE_CHUNK_SIZE * 3))

    session._pty.write.assert_not_called()


@pytest.mark.asyncio
async def test_empty_write_is_noop():
    """Empty payloads must not trigger a spurious PTY call."""
    loop = asyncio.get_running_loop()
    session = _make_session(loop)

    session.write("")

    session._pty.write.assert_not_called()


@pytest.mark.asyncio
async def test_write_swallows_pty_exception():
    """A PTY-side failure must not propagate — write is best-effort
    (matches the prior contract and audit-log behaviour)."""
    loop = asyncio.get_running_loop()
    session = _make_session(loop)
    session._pty.write.side_effect = OSError("pipe gone")

    # Must not raise.
    session.write("hello")
    session.write("x" * (_WRITE_CHUNK_SIZE * 2))
