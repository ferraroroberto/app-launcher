"""Real-PTY readback integration test for #64.

The reopened #64 showed why ``test_session_host_pty_write.py`` (which
asserts against a ``MagicMock`` PtyProcess) cannot catch byte loss: a mock
can never drop bytes, so ``"".join(calls) == payload`` is trivially true.
This test pushes multi-KB payloads through ``PtySession.write`` into a
*real* ConPTY and reads back what the child actually received off the
pseudoconsole, asserting a byte-for-byte lossless delivery — the guard the
issue demanded ("a real-PTY ... lossless readback (mock-only coverage is
insufficient and must not be the only guard)").

Note on scope: this proves the *write boundary* (``PtySession.write`` →
pywinpty → the ConPTY input pipe) is lossless. The real-device truncation
#64 reopened on is consumer-side — the agent's TUI absorbing a synthesized
keystroke burst via the Windows console input queue — and is addressed by
the browser-side bracketed-paste framing (``terminal.js`` ``framePaste``);
that path needs on-device verification and cannot be reproduced faithfully
in-vitro (a raw pipe reader never exhibits the console-queue drop).

Windows + pywinpty only; skips cleanly elsewhere.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from src.session_host import PtyProcess, PtySession
from tests._pty_wait import DELIVERY_CEILING_S, READY_CEILING_S, await_until

pytestmark = pytest.mark.skipif(
    PtyProcess is None, reason="pywinpty (Windows ConPTY) is required"
)

_CHILD = Path(__file__).parent / "_pty_readback_child.py"
_SENTINEL = "<<<EOP>>>"


async def _readback(size: int, tmp_path: Path) -> tuple[str, str]:
    """Spawn the raw-reading child in a real ConPTY, push a ``size``-char
    payload through ``PtySession.write``, and return (sent, received)."""
    result = tmp_path / "readback.bin"
    ready = Path(str(result) + ".ready")
    # The destination goes in the environment, not in argv (#822). pywinpty's
    # string-command spawn tokenises with `shlex.split(cmd, posix=False)`, so a
    # positional path is only as safe as the assumption that it contains no
    # space — and when that assumption broke, the child wrote to a truncated
    # prefix of the path instead of failing. An env var is not tokenised.
    # Only the executable and the child script remain positional; both are
    # repo-controlled paths.
    cmd = f"{sys.executable} {_CHILD}"
    env = {**os.environ, "PTY_READBACK_RESULT": str(result)}
    pty = PtyProcess.spawn(cmd, cwd=str(_CHILD.parent), dimensions=(40, 120), env=env)
    loop = asyncio.get_running_loop()
    session = PtySession(
        session_id="readback",
        project_dir=str(_CHILD.parent),
        name="child",
        flags="",
        started_at=time.time(),
        _loop=loop,
        _pty=pty,
    )
    session.start_reader()
    try:
        # Wait until the child has switched stdin to raw mode. A polled
        # condition with a backstop, never a fixed budget (#1108): a cold
        # interpreter inside a fresh ConPTY costs 3.1 s here even on an idle
        # box, so any hand-picked tick count is a flake waiting for load.
        await await_until(
            ready.exists,
            ceiling_s=READY_CEILING_S,
            what="the child never signalled raw-mode readiness",
            env_var="PTY_READY_CEILING_S",
        )

        # Distinct, newline-free characters so any dropped span shows up as
        # a length delta and a mid-stream divergence, not a benign reflow.
        payload = "".join(chr(0x41 + (i % 26)) for i in range(size))
        session.write(payload + _SENTINEL)

        def delivered() -> bool:
            """True once there is nothing further to wait for.

            Two ways that happens, and conflating them is what made a slow
            child look like byte loss. Either the file is complete, or the
            child has exited — and a child that exited having written a
            *short* file is precisely the loss this test exists to catch, so
            stop waiting and let the assertions below report the delta
            instead of burning the ceiling on a defect already proven.
            """
            if result.exists() and result.stat().st_size >= len(payload):
                return True
            return not pty.isalive()

        await await_until(
            delivered,
            ceiling_s=DELIVERY_CEILING_S,
            what="the child never finished writing the readback file",
            env_var="PTY_DELIVERY_CEILING_S",
            detail=lambda: (
                f"{result.stat().st_size if result.exists() else 0} "
                f"of {len(payload)} bytes had landed, child still alive"
            ),
        )
    finally:
        try:
            pty.close(force=True)
        except Exception:
            pass

    received = result.read_bytes().decode("utf-8", "replace") if result.exists() else ""
    return payload, received


@pytest.mark.parametrize("size", [2048, 5120, 10240])
async def test_long_write_delivers_into_real_pty_losslessly(
    size: int, tmp_path: Path
) -> None:
    sent, received = await _readback(size, tmp_path)
    assert len(received) == len(sent), (
        f"{size}-char write lost bytes at the PTY boundary: "
        f"delivered {len(received)} of {len(sent)}"
    )
    assert received == sent, "delivered bytes diverge from the payload (dropped span)"
