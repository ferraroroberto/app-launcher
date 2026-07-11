"""Real-Codex semantic submit regression for issue #436.

The browser-level compose tests prove WebSocket framing, but that did not prove
that Codex interpreted the final carriage return as Submit.  On Windows,
Codex's fallback paste-burst detector reclassified the launcher's already
bracketed bulk write and intentionally converted the first Enter to a newline.

This test launches a real Codex ConPTY with the compatibility override, pastes
``/quit`` through the production framing, and asserts that one carriage return
actually exits the TUI.  It makes no model request.  Machines without Codex (CI
included) skip cleanly.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import pytest

from src.session_host import PtyProcess, SessionManager

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or PtyProcess is None or shutil.which("codex") is None,
    reason="Windows, pywinpty, and Codex CLI are required",
)


async def test_bracketed_prompt_plus_one_enter_semantically_submits_to_codex(
) -> None:
    manager = SessionManager()
    manager.attach_loop(asyncio.get_running_loop())
    session = manager.create(
        # Codex shows an interactive trust prompt for a brand-new pytest temp
        # directory, which would test that prompt rather than the composer.
        # The checked-out repo is the same trusted project used by the app's
        # own full-control launches.
        str(Path(__file__).resolve().parents[1]),
        "codex-submit-probe",
        "-c disable_paste_burst=true",
        agent="codex",
        rows=40,
        cols=100,
    )
    try:
        # Answer the terminal identity query and allow the TUI to finish its
        # first paint before exercising input.  This is the same handshake an
        # attached xterm performs; no model call is involved.
        await asyncio.sleep(1.0)
        session.write("\x1b[?1;2c")
        session.write("\x1b[I")
        await asyncio.sleep(2.0)

        session.write("\x1b[200~/quit\x1b[201~")
        session.write("\r")

        for _ in range(30):
            if not session.alive:
                break
            await asyncio.sleep(0.1)
        assert not session.alive, (
            "one Enter left /quit in Codex's composer instead of submitting it"
        )
    finally:
        if session.alive:
            session.stop(mode="kill")
