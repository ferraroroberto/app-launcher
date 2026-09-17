"""Real-Claude Esc regression for issue #987.

The keys popover's Esc sends a bare ``\\x1b`` (pinned in the browser by
``tests/e2e/test_keys_popover.py::test_esc_key_sends_bare_escape_and_closes``).
This is the other half: that byte, written through the session-host's PTY
path into a real Claude Code ConPTY, is one Claude acts on as Esc. ConPTY
runs in win32-input-mode (``?9001h``), where a legacy VT byte could in
principle be misread — #269 saw exactly that class on Codex — so the
encoding is proven empirically, not assumed.

#987's diagnosis (recorded on the issue): no hop dropped the byte. The
on-device report came from a Grok session, and Grok answers Esc during a
turn with "Press Ctrl+c to cancel the turn"; Claude interrupts on it.

Esc is observed as **the slash-command menu closing** — Claude Code's own
local reaction, so no model call is involved. Machines without the Claude
CLI (CI included) skip cleanly.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path
from typing import List

import pytest

from src.session_host import PtyProcess, PtySession, SessionManager
from src.vt_snapshot import VtSnapshot
from tests.test_claude_pty_submit import (
    _COMPOSER_MARKERS,
    _RULE_MIN_DASHES,
    _live_rows,
    _wait_for_any,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or PtyProcess is None or shutil.which("claude") is None,
    reason="Windows, pywinpty, and the Claude Code CLI are required",
)

# Typing this opens the slash-command menu under the composer, listing /help.
_PARTIAL_COMMAND = "/hel"
_MENU_ENTRY = "/help"
# The Esc key exactly as terminal-keys-bytes.js sends it.
_ESC = "\x1b"


def _below_composer(session: PtySession) -> List[str]:
    """Rows under the composer's closing rule — where the menu renders.

    Empty when the rules can't be located, so a parse miss reads as "no menu"
    and fails the non-vacuous precondition below instead of passing.
    """
    rows = _live_rows(session.snapshot_frame() or "")
    rules = [i for i, row in enumerate(rows) if row.count("─") >= _RULE_MIN_DASHES]
    below = rows[rules[-1] + 1 :] if len(rules) >= 2 else []
    return [row.rstrip() for row in below if row.strip()]


def _menu_open(session: PtySession) -> bool:
    return any(_MENU_ENTRY in row for row in _below_composer(session))


async def _wait_menu(session: PtySession, open_: bool, budget_s: float) -> bool:
    """Poll until the menu's state is ``open_`` for three consecutive reads.

    Three reads, because a frame caught mid-repaint can momentarily show
    either state.
    """
    streak = 0
    for _ in range(int(budget_s / 0.1)):
        streak = streak + 1 if _menu_open(session) == open_ else 0
        if streak >= 3:
            return True
        if not session.alive:
            return False
        await asyncio.sleep(0.1)
    return False


async def test_bare_escape_reaches_claude_through_the_pty() -> None:
    manager = SessionManager()
    manager.attach_loop(asyncio.get_running_loop())
    session = manager.create(
        # The checked-out repo is trusted; a fresh temp dir would hit the
        # trust prompt (same reasoning as test_claude_pty_submit).
        str(Path(__file__).resolve().parents[1]),
        "claude-esc-probe",
        "",
        agent="claude",
        rows=40,
        cols=100,
    )
    try:
        await asyncio.sleep(1.0)
        session.write("\x1b[?1;2c")
        session.write("\x1b[I")
        vt = VtSnapshot(40, 100)
        with session._ring_lock:
            vt.feed(session._ring)
            session._vt = vt

        assert await _wait_for_any(session, _COMPOSER_MARKERS, 30.0), (
            "Claude Code composer never became ready within 30 s"
        )
        await asyncio.sleep(1.0)

        for ch in _PARTIAL_COMMAND:
            session.write(ch)
        # Non-vacuous precondition: the menu must be up, or its absence after
        # Esc proves nothing.
        assert await _wait_menu(session, True, 15.0), (
            f"typing {_PARTIAL_COMMAND!r} never opened Claude's slash-command "
            "menu within 15 s, so the Esc assertion below would pass vacuously; "
            f"rows below the composer: {_below_composer(session)!r}"
        )

        session.write(_ESC)

        if not await _wait_menu(session, False, 10.0):
            pytest.fail(
                "a bare \\x1b written to the Claude Code PTY did not close the "
                "slash-command menu within 10 s — Claude is not reading the "
                "popover's Esc as Esc (issue #987). Rows below the composer: "
                + repr(_below_composer(session))
            )
    finally:
        if session.alive:
            session.stop(mode="kill")
