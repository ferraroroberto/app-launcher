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
carries new or changed lifecycle hooks (issue #462), and a separate
"Update available!" startup modal appears whenever a newer Codex CLI release
exists (issue #485).  Either interstitial paints over the composer and
captures Enter, so the probe disables update discovery and hooks for this
process before reaching the composer it is meant to verify.  The submit
contract itself is unchanged — once the composer is reached, one bracketed
paste plus one carriage return still submits.  These test-only launch options
neither persist a preference nor change hook trust.

Timing (issue #493): the CR is sent only after the pasted text has visibly
rendered in the composer, and the exit wait is a polled 15 s budget — a fixed
3 s budget flaked ~2/20 under concurrent load (typical submit->exit is
~870 ms, but the full pre-ship gate can push it past 3 s).
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

# Text that only appears once the composer banner has painted.  Note that the
# composer box paints in the background even while an overlay modal/panel sits
# on top of it, so this alone does not prove input will land in the composer —
# the modal markers below must be checked first on every poll.
_COMPOSER_MARKER = "OpenAI Codex"
# The "Update available!" startup modal (issue #485) appears whenever a newer
# Codex CLI release exists.  "2. Skip" dismisses it for this run only, without
# running the update or persisting a "skip until next version" preference.
# Matched on its "Press enter to continue" prompt rather than the "Update
# available!" title: that title stays painted as a static banner above the
# composer even after the modal itself is dismissed, so matching on it would
# make this branch fire forever and starve the hooks/composer checks below.
_UPDATE_MODAL_MARKER = "Press enter to continue"
# The hooks-review startup panel (issue #462, reshaped since — issue #485)
# whenever ``~/.codex`` carries new or changed lifecycle hooks.  Matched
# case-insensitively since the exact phrasing/casing has already changed once
# ("Hooks need review" -> "N hooks need review"). "esc to close" dismisses it
# without trusting anything.
_HOOKS_MODAL_MARKER = "hooks need review"


async def _reach_composer(session: PtySession) -> bool:
    """Poll until the Codex composer is up, clearing startup interstitials.

    Returns ``True`` once the composer banner is visible with no known modal
    on top of it. The "Update available!" modal is dismissed with
    ``2<Enter>`` ("Skip"); the hooks-review panel is dismissed with ``Esc``
    ("close" without trusting). Modals are checked before the composer marker
    on every poll, since the composer banner paints in the background even
    while a modal overlays it. Bounded so a genuinely stuck TUI fails the
    assertion instead of hanging.
    """
    for _ in range(75):  # ~15 s ceiling at 0.2 s per poll, two modals to clear
        frame = session.snapshot_frame() or ""
        if _UPDATE_MODAL_MARKER in frame:
            # Numbered-select modal: digit moves the cursor, Enter confirms.
            session.write("2\r")
        elif _HOOKS_MODAL_MARKER in frame.lower():
            session.write("\x1b")
        elif _COMPOSER_MARKER in frame:
            return True
        if not session.alive:
            return False
        await asyncio.sleep(0.2)
    frame = session.snapshot_frame() or ""
    return (
        _UPDATE_MODAL_MARKER not in frame
        and _HOOKS_MODAL_MARKER not in frame.lower()
        and _COMPOSER_MARKER in frame
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
        # Keep release availability and installed user hooks out of this
        # semantic-submit proof.  All overrides apply only to the spawned test
        # process; user and production Codex configuration remains unchanged.
        "-c disable_paste_burst=true "
        "-c check_for_update_on_startup=false --disable hooks",
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

        # Clear any startup interstitial (Codex 0.144.x hooks-review panel,
        # #462; update-available modal, #485) so the paste lands in the
        # composer, not a modal.
        assert await _reach_composer(session), (
            "Codex composer never became ready — a startup interstitial other "
            "than the known hooks-review or update-available modals may be "
            "blocking input"
        )
        # Let the composer finish settling before pasting.  The banner paints a
        # beat before the input is ready to treat a trailing CR as Submit; on a
        # loaded host (e.g. the full pre-ship suite) pasting immediately — most
        # of all right after dismissing the modal — races that and the Enter is
        # swallowed.  This is the settle the original fixed sleep provided.
        await asyncio.sleep(2.0)

        session.write("\x1b[200~/quit\x1b[201~")
        # Only send the CR once the composer has visibly rendered the paste
        # (issue #493): on a loaded host the CR can otherwise land while Codex
        # is still processing the bracketed paste and be treated as a newline
        # instead of Submit.  Typical render latency is ~65 ms (timing probe,
        # #493); the ceiling only matters under heavy load.
        for _ in range(100):  # 10 s ceiling
            if "/quit" in (session.snapshot_frame() or ""):
                break
            if not session.alive:
                pytest.fail("Codex died before the pasted /quit rendered")
            await asyncio.sleep(0.1)
        else:
            pytest.fail(
                "pasted /quit never rendered in the Codex composer within 10 s"
            )
        session.write("\r")

        # Typical submit->exit latency is ~870 ms (timing probe, #493); the
        # old fixed 3 s budget was only ~3.5x that and was exceeded under the
        # full pre-ship gate's load.  Polled, so healthy runs still finish in
        # under a second.
        for _ in range(150):  # 15 s ceiling
            if not session.alive:
                break
            await asyncio.sleep(0.1)
        final_frame = session.snapshot_frame() or ""
        assert not session.alive, (
            "one Enter did not exit Codex within 15 s — "
            + (
                "/quit is still in the composer (Enter was swallowed)"
                if "/quit" in final_frame
                else "/quit left the composer (submitted, but exit overran the budget)"
            )
        )
    finally:
        if session.alive:
            session.stop(mode="kill")
