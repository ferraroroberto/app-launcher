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
import threading
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

    delivered = session.write("hello")

    assert delivered is True
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

    delivered = session.write(payload)

    assert delivered is True
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
    """A write into a dead session must not touch the (possibly closed) PTY,
    and must report the drop (issue #607) rather than silently succeeding —
    the exited session can still be sitting in manager.get() for up to the
    30s reap window, so a caller relies on this to know the message never
    landed."""
    loop = asyncio.get_running_loop()
    session = _make_session(loop)
    session._exited = True

    delivered = session.write("x" * (_WRITE_CHUNK_SIZE * 3))

    assert delivered is False
    session._pty.write.assert_not_called()


@pytest.mark.asyncio
async def test_empty_write_is_noop():
    """Empty payloads must not trigger a spurious PTY call, and trivially
    succeed — there was nothing to deliver, so nothing was dropped."""
    loop = asyncio.get_running_loop()
    session = _make_session(loop)

    delivered = session.write("")

    assert delivered is True
    session._pty.write.assert_not_called()


@pytest.mark.asyncio
async def test_write_swallows_pty_exception_but_reports_failure():
    """A PTY-side failure must not propagate — write is best-effort
    (matches the prior contract and audit-log behaviour) — but must now
    report the drop via its return value (issue #607) instead of silently
    claiming success."""
    loop = asyncio.get_running_loop()
    session = _make_session(loop)
    session._pty.write.side_effect = OSError("pipe gone")

    # Must not raise, but must report the drop.
    assert session.write("hello") is False
    assert session.write("x" * (_WRITE_CHUNK_SIZE * 2)) is False


# --------------------------------------------------------------------------
# Issue #721: two concurrent writers on one PTY.
#
# Since #611 the session-host has had two independent write paths — the
# WebSocket pump (``app/session_host/server.py`` per-message
# ``asyncio.to_thread(session.write, …)``) and the HTTP ``/input`` route
# (``asyncio.to_thread(session.submit_input, …)``) — and a PC mirror plus a
# phone attached to the same session at once is the documented, intended
# shape. Nothing serialized them: ``_ring_lock`` guards the *output* ring and
# the manager's lock guards the registry. So a keystroke could land between
# two ~512 B chunks of somebody else's paste, or between a paste and its
# submitting CR.
# --------------------------------------------------------------------------


def _slow_write_session(
    loop, per_write_s: float = 0.005
) -> tuple[PtySession, list[str]]:
    """A session whose PTY write is slow enough that an unserialized second
    writer is near-certain to interleave, and which records call order."""
    session = _make_session(loop)
    calls: list[str] = []
    record_lock = threading.Lock()

    def _record(chunk: str) -> None:
        time.sleep(per_write_s)
        with record_lock:
            calls.append(chunk)

    session._pty.write.side_effect = _record
    return session, calls


@pytest.mark.asyncio
async def test_concurrent_writes_do_not_interleave_chunks():
    """A single keystroke arriving mid-paste must land wholly before or
    wholly after it — never spliced between the paste's own chunks."""
    loop = asyncio.get_running_loop()
    session, calls = _slow_write_session(loop)
    paste = "A" * (_WRITE_CHUNK_SIZE * 4)  # 4 chunks, paced

    paster = threading.Thread(target=session.write, args=(paste,))
    paster.start()
    time.sleep(0.008)  # land the keystroke while the paste is mid-flight
    session.write("B")
    paster.join()

    joined = "".join(calls)
    assert joined in (paste + "B", "B" + paste), (
        "a concurrent single-char write interleaved with a chunked paste — "
        f"got {len(calls)} chunks in an order that splices them: {joined[:40]}…"
    )


@pytest.mark.asyncio
async def test_concurrent_write_cannot_land_between_paste_and_its_cr(monkeypatch):
    """``submit_input`` writes the text, waits for the paste to settle, then
    writes the submitting CR as a *separate* PTY write (#166). A byte landing
    in that gap turns the CR into a literal newline inside another user's
    message, so the lock has to span the whole sequence, not just each
    ``write()`` call."""
    import src.session_host as sh

    # Keep the settle wait short, and make it actually *settle*: since #763 a
    # bulk payload that never goes quiet no longer gets a CR at the cap at all
    # (it is handed to the deferred watcher), so this test has to drive the
    # settled path to have a CR to assert the lock spans.
    monkeypatch.setattr(sh, "_BULK_FLOOR_MS", 50)
    monkeypatch.setattr(sh, "_BULK_QUIET_MS", 50)
    monkeypatch.setattr(sh, "_BULK_CAP_MS", 2000)
    monkeypatch.setattr(sh, "_INGEST_CAP_MS", 3000)

    loop = asyncio.get_running_loop()
    session, calls = _slow_write_session(loop, per_write_s=0.001)
    payload = "A" * 600  # >= _BULK_SUBMIT_THRESHOLD_CHARS -> takes the settle path

    submitter = threading.Thread(
        target=session.submit_input, args=(payload, True)
    )
    submitter.start()
    time.sleep(0.02)
    # Stand in for the reader thread painting the paste back, so the CR is
    # actually reached — without ingest evidence #760 withholds it.
    with session._ring_lock:
        session._output_total += len(payload)
        session._ring += payload
    session._last_output_at = time.time()  # the paint the quiet wait measures
    time.sleep(0.03)  # squarely inside the settle wait
    session.write("B")
    submitter.join()

    joined = "".join(calls)
    assert joined == payload + "\r" + "B", (
        "a concurrent write landed between the paste and its submitting CR "
        f"(tail was {joined[-4:]!r}) — the lock must span text→settle→CR, "
        "not just each write() call"
    )
