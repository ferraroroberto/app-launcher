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
import sys
import time
from pathlib import Path

import pytest

from src.session_host import PtyProcess, PtySession

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
    # No quotes around the paths: pywinpty's string-command spawn does its
    # own tokenising that doesn't honour double-quotes the way CreateProcess
    # would (a quoted path arrives mangled). The venv python, this test dir,
    # and the pytest tmp_path are all space-free, so bare tokens are safe.
    cmd = f"{sys.executable} {_CHILD} {result}"
    pty = PtyProcess.spawn(cmd, cwd=str(_CHILD.parent), dimensions=(40, 120))
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
        # Wait until the child has switched stdin to raw mode.
        for _ in range(100):
            if ready.exists():
                break
            await asyncio.sleep(0.05)
        assert ready.exists(), "child never signalled raw-mode readiness"

        # Distinct, newline-free characters so any dropped span shows up as
        # a length delta and a mid-stream divergence, not a benign reflow.
        payload = "".join(chr(0x41 + (i % 26)) for i in range(size))
        session.write(payload + _SENTINEL)

        for _ in range(200):
            await asyncio.sleep(0.05)
            if result.exists() and result.stat().st_size >= len(payload):
                break
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
