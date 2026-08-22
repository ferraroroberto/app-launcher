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
actually submits it.  Submit is observed as **the paste leaving the
composer** — Claude Code's own local, synchronous reaction to Enter, so the
assertion needs no model round trip and is not coupled to the wording of
whatever answer comes back.  The payload is framed as an unknown slash
command (``/probe-499-nonexistent ...``) purely so that a submission is
harmless; it used to double as a local-answer trick ("Unknown slash
command"), but Claude Code v2.1.225 routes unknown slash commands to the
model instead, which silently turned that signal into a permanent red and a
misleading "CR was swallowed" diagnostic (issue #728).  Machines without the
Claude CLI (CI included) skip cleanly.

Timing: like the Codex sibling (issue #493), the CR is sent only once the
pasted payload has visibly rendered in the composer, and the response wait is
a polled budget — this asserts the submit *contract* deterministically; the
launcher's own protection for the no-render-wait production path is the
size-thresholded CR defer in ``terminal-compose.js`` (issue #499), calibrated
with the loaded-probe loop recorded on the issue.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from src.session_host import PtyProcess, PtySession, SessionManager
from src.vt_snapshot import VtSnapshot

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or PtyProcess is None or shutil.which("claude") is None,
    reason="Windows, pywinpty, and the Claude Code CLI are required",
)

# Composer-ready markers. The placeholder hint ("> Try \"edit <filepath> to...\"")
# used to be the sole marker, but it is only shown for the first ~1s before the
# status line grows a second row and displaces it (#777) — load makes that
# window shrink further, producing an intermittent false "never became ready".
# Use a tuple of alternatives instead of one literal, so a future CLI cosmetic
# change degrades to "one of these still matched" rather than a red:
#   1. the startup version banner ("Claude Code v...") — printed once at the
#      top of the header and, empirically, repainted on every full frame from
#      the first non-empty paint onward, never suppressed or rotated away.
#   2. the "auto mode on" status-line hint under the composer.
#   3. the composer prompt glyph ("❯") itself.
# Verified against Claude Code CLI v2.1.240 (2026-08-22).
_COMPOSER_MARKERS = ("Claude Code v", "auto mode on", "❯")
# Unknown-slash-command payload: submitting it is harmless.  One long line,
# no newlines — the shape of a real dictation transcript (#499).
_PAYLOAD = "/probe-499-nonexistent " + (
    "the quick brown fox jumps over the lazy dog and keeps narrating " * 30
).strip()
# Anything still sitting in the composer means the CR never submitted — the
# payload renders either literally or as a collapsed chip (#499's loaded probe
# saw both).
_IN_COMPOSER = ("probe-499-nonexistent", "[Pasted text")
# ``VtSnapshot.render()`` paints the live frame with one absolute
# ``ESC[<row>;1H`` per row, after the plain-text scrollback history (#432).
_FRAME_ROW = re.compile(r"\x1b\[(\d+);1H")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# A composer border is a full-width run of box-drawing dashes; 20 is well
# above anything that turns up inside real content.
_RULE_MIN_DASHES = 20


def _live_rows(frame: str) -> List[str]:
    """The rows of the *current screen* out of a rendered frame.

    Splitting on the absolute cursor-position escapes drops the scrolled-off
    history ``render()`` prepends, which is what "what is on screen right
    now" has to mean here — after a submit the payload is still in the
    transcript above, and only its absence from the composer proves anything.
    """
    parts = _FRAME_ROW.split(frame)
    rows: Dict[int, str] = {}
    for i in range(1, len(parts) - 1, 2):
        rows[int(parts[i])] = _ANSI.sub("", parts[i + 1])
    return [rows[key] for key in sorted(rows)]


def _composer_text(frame: str) -> str:
    """The text sitting in Claude Code's composer box right now.

    The composer is the region between the last two horizontal rules of the
    live frame — below them sit only the model/branch status line and the
    hint line. Falls back to the whole frame when the rules can't be located,
    so a parse miss keeps the caller waiting rather than reporting a false
    submit.
    """
    rows = _live_rows(frame)
    rules = [i for i, row in enumerate(rows) if row.count("─") >= _RULE_MIN_DASHES]
    if len(rules) < 2:
        return "\n".join(rows) or frame
    return "\n".join(rows[rules[-2] + 1 : rules[-1]])


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

        assert await _wait_for_any(session, _COMPOSER_MARKERS, 30.0), (
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

        # The composer must be holding the payload right now, or the assertion
        # below would pass without ever proving a submit happened.
        before = _composer_text(session.snapshot_frame() or "")
        assert any(marker in before for marker in _IN_COMPOSER), (
            "the pasted payload is not in the composer before the CR is sent, "
            "so the submit assertion below would pass vacuously; composer "
            "reads as: " + repr(before[:200])
        )

        session.write("\r")

        # Submitted = the payload left the composer. Enter empties the composer
        # locally and synchronously, before any answer comes back, so this
        # proves the submit itself without a model round trip and without
        # depending on how a given Claude Code version words its reply (#728).
        # A swallowed CR — the #499 defect this test exists for — leaves the
        # payload sitting exactly where it was.
        #
        # Three consecutive clear reads, because a frame caught mid-repaint can
        # momentarily show an empty composer while the paste is still there.
        clear_reads = 0
        submitted = False
        for _ in range(150):  # 15 s ceiling
            composer = _composer_text(session.snapshot_frame() or "")
            if any(marker in composer for marker in _IN_COMPOSER):
                clear_reads = 0
            else:
                clear_reads += 1
                if clear_reads >= 3:
                    submitted = True
                    break
            if not session.alive:
                break
            await asyncio.sleep(0.1)
        if not submitted:
            pytest.fail(
                "one Enter did not submit the bulk paste within 15 s — the "
                "payload is still sitting in the composer, so the CR was "
                "swallowed (issue #499). Composer reads as: "
                + repr(_composer_text(session.snapshot_frame() or "")[:200])
            )
    finally:
        if session.alive:
            session.stop(mode="kill")
