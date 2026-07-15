"""Real-Claude semantic submit regression for issue #499.

The browser-level compose tests prove WebSocket framing, but #499 showed the
framing is not the whole contract on Claude either: a dictation-sized
bracketed paste followed by an immediate carriage return can land the CR
while Claude Code is still ingesting the paste, and the CR becomes a newline
into the composer instead of Submit.  The swallow is load-dependent (#493
measured 5x latency spikes under concurrent PTY load), which is why #490's
idle-machine probe missed it.

This test launches a real Claude Code ConPTY, pastes a dictation-sized
payload through the production framing, and asserts that one carriage return
actually submits it.  The payload is framed as an unknown slash command
(``/probe-499-nonexistent ...``) so Claude Code answers locally ("Unknown
slash command") — submit is observable with no model request.  Machines
without the Claude CLI (CI included) skip cleanly.

Timing: like the Codex sibling (issue #493), the CR is sent only once the
pasted payload has visibly rendered in the composer, and the response wait is
a polled budget — this asserts the submit *contract* deterministically; the
launcher's own protection for the no-render-wait production path is the
size-thresholded CR defer in ``terminal-compose.js`` (issue #499), calibrated
with the loaded-probe loop recorded on the issue.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path
from typing import Tuple

import pytest

from src.session_host import PtyProcess, PtySession, SessionManager
from src.vt_snapshot import VtSnapshot

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or PtyProcess is None or shutil.which("claude") is None,
    reason="Windows, pywinpty, and the Claude Code CLI are required",
)

# The composer's rotating placeholder hint ("> Try \"edit <filepath> to...\"").
# NB: Claude renders a NO-BREAK SPACE (U+00A0) after the ">", so the marker
# must not span that gap.  Present on every fresh composer paint.
_COMPOSER_MARKER = 'Try "'
# Unknown-slash-command payload: Claude Code answers it locally, so the probe
# proves Submit without any model call.  One long line, no newlines — the
# shape of a real dictation transcript (#499).
_PAYLOAD = "/probe-499-nonexistent " + (
    "the quick brown fox jumps over the lazy dog and keeps narrating " * 30
).strip()


async def _wait_for_any(
    session: PtySession, markers: Tuple[str, ...], budget_s: float
) -> bool:
    """Poll the VT frame until any of ``markers`` appears (case-sensitive)."""
    for _ in range(int(budget_s / 0.1)):
        frame = session.snapshot_frame() or ""
        if any(m in frame for m in markers):
            return True
        if not session.alive:
            return False
        await asyncio.sleep(0.1)
    frame = session.snapshot_frame() or ""
    return any(m in frame for m in markers)


async def test_bracketed_bulk_paste_plus_one_enter_semantically_submits_to_claude(
) -> None:
    manager = SessionManager()
    manager.attach_loop(asyncio.get_running_loop())
    session = manager.create(
        # A brand-new pytest temp directory would hit Claude's trust prompt;
        # the checked-out repo is the same trusted project the app's own
        # launches use.
        str(Path(__file__).resolve().parents[1]),
        "claude-submit-probe",
        "",
        agent="claude",
        rows=40,
        cols=100,
    )
    # Claude is a non-fullscreen agent so SessionManager wires no VT mirror;
    # attach one so snapshot_frame() renders the live screen for the probe.
    session._vt = VtSnapshot(40, 100)
    try:
        # Answer the terminal identity query and let the TUI finish its first
        # paint before exercising input — same handshake an attached xterm
        # performs; no model call is involved.
        await asyncio.sleep(1.0)
        session.write("\x1b[?1;2c")
        session.write("\x1b[I")

        assert await _wait_for_any(session, (_COMPOSER_MARKER,), 30.0), (
            "Claude Code composer never became ready within 30 s"
        )
        # Let the composer finish settling after the banner paints (same
        # settle the Codex sibling needs on a loaded host).
        await asyncio.sleep(1.0)

        session.write("\x1b[200~" + _PAYLOAD + "\x1b[201~")
        # Only send the CR once the composer has visibly rendered the paste
        # (#493's mitigation shape): this asserts the submit contract itself,
        # not the no-render-wait race — that is what the production CR defer
        # in terminal-compose.js exists for (#499). A dictation-sized paste
        # renders as a collapsed "[Pasted text #N]" chip, not literal text
        # (observed in the #499 loaded-probe loop), so accept either form.
        assert await _wait_for_any(
            session, ("probe-499-nonexistent", "[Pasted text"), 15.0
        ), "pasted payload never rendered in the Claude composer within 15 s"
        # The chip render is not the end of the paste ingest — the #499 loop
        # showed a CR landing right after the chip paints still gets absorbed.
        # Hold the CR back the way the production Send does (the #499
        # bulk-settle watch in terminal-compose.js): output has arrived, so
        # now wait for the output stream to go quiet for 350 ms, capped.
        # The probe measured this echo-then-quiet protocol at 20/20 under
        # synthetic load where fixed 350/1000 ms defers were each 19/20.
        quiet_since = asyncio.get_running_loop().time()
        last_len = len(session._ring)
        cap = quiet_since + 5.0
        while asyncio.get_running_loop().time() < cap:
            now = asyncio.get_running_loop().time()
            cur = len(session._ring)
            if cur != last_len:
                last_len = cur
                quiet_since = now
            elif now - quiet_since >= 0.35:
                break
            await asyncio.sleep(0.05)
        session.write("\r")

        # Submitted = Claude answered the unknown slash command locally.
        # Checked against the raw output ring, not just the current VT frame
        # — under load the error line can scroll or clear off-screen before
        # a poll sees it (observed in the #499 probe loop). Swallowed = the
        # payload (or its chip) is still sitting in the composer.
        submitted = False
        for _ in range(150):  # 15 s ceiling
            if "Unknown" in session._ring:
                submitted = True
                break
            if not session.alive:
                break
            await asyncio.sleep(0.1)
        if not submitted:
            frame = session.snapshot_frame() or ""
            pytest.fail(
                "one Enter did not submit the bulk paste within 15 s — "
                + (
                    "payload still in the composer (CR was swallowed)"
                    if "probe-499-nonexistent" in frame or "[Pasted text" in frame
                    else "payload left the composer but no local response rendered"
                )
            )
    finally:
        if session.alive:
            session.stop(mode="kill")
