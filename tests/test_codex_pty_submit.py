"""Real-Codex semantic submit regression for issue #436.

The browser-level compose tests prove WebSocket framing, but that did not prove
that Codex interpreted the final carriage return as Submit.  On Windows,
Codex's fallback paste-burst detector reclassified the launcher's already
bracketed bulk write and intentionally converted the first Enter to a newline.

This test launches a real Codex ConPTY with the compatibility override, pastes
``/quit`` through the production framing, and asserts that one carriage return
actually exits the TUI.  It makes no model request.  Machines without Codex (CI
included) skip cleanly.

Codex 0.144.x adds a "Hooks need review" startup modal whenever ``~/.codex``
carries new or changed lifecycle hooks (issue #462).  That interstitial paints
over the composer and captures Enter, so the probe must clear it before the
composer it is meant to test even exists.  The submit contract itself is
unchanged — once the composer is reached, one bracketed paste plus one carriage
return still submits.  We therefore dismiss the modal when present (a
non-mutating "continue without trusting" choice) rather than adjusting the
submit sequence.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import pytest

from src.session_host import PtyProcess, PtySession, SessionManager

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or PtyProcess is None or shutil.which("codex") is None,
    reason="Windows, pywinpty, and Codex CLI are required",
)

# Text that only appears once the composer banner has painted.  Reaching it is
# how we know any startup interstitial is gone and input will land in the
# composer rather than a modal.
_COMPOSER_MARKER = "OpenAI Codex"
# The Codex 0.144.x startup interstitial (issue #462); its number-select list
# offers "3. Continue without trusting", the least-side-effect, non-persisting
# dismissal.
_HOOKS_MODAL_MARKER = "Hooks need review"


async def _reach_composer(session: PtySession) -> bool:
    """Poll until the Codex composer is up, clearing a startup interstitial.

    Returns ``True`` once the composer banner is visible.  If a "Hooks need
    review" modal is on screen it is dismissed with ``3<Enter>`` ("continue
    without trusting" — per-session, mutates nothing).  Bounded so a genuinely
    stuck TUI fails the assertion instead of hanging.
    """
    for _ in range(50):  # ~10 s ceiling at 0.2 s per poll
        frame = session.snapshot_frame() or ""
        if _COMPOSER_MARKER in frame:
            return True
        if _HOOKS_MODAL_MARKER in frame:
            # Numbered-select modals accept the digit to move the cursor and
            # Enter to confirm.  Only sent while the modal is actually up, so a
            # composer-only run never receives these keystrokes.
            session.write("3\r")
        if not session.alive:
            return False
        await asyncio.sleep(0.2)
    return _COMPOSER_MARKER in (session.snapshot_frame() or "")


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

        # Clear any startup interstitial (Codex 0.144.x hooks-review modal,
        # #462) so the paste lands in the composer, not a modal.
        assert await _reach_composer(session), (
            "Codex composer never became ready — a startup interstitial other "
            "than the known hooks-review modal may be blocking input"
        )
        # Let the composer finish settling before pasting.  The banner paints a
        # beat before the input is ready to treat a trailing CR as Submit; on a
        # loaded host (e.g. the full pre-ship suite) pasting immediately — most
        # of all right after dismissing the modal — races that and the Enter is
        # swallowed.  This is the settle the original fixed sleep provided.
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
