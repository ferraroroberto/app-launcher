"""Regression pin for #64 — bracketed-paste framing of phone pastes.

The reopened #64: a multi-KB clipboard paste from the phone (📋 button or
compose ➤ Send) lost spans mid-stream, because the payload was delivered to
the agent's TUI as a raw keystroke burst the Windows console input queue
drops under load. Fix: ``terminal.js`` ``framePaste`` wraps the payload in
bracketed-paste markers (DECSET 2004) when — and only when — the agent has
enabled them, so the TUI buffers it as one atomic paste. This mirrors what
xterm already does for its own native paste; the phone buttons bypass xterm
and so replicate it.

This pins the framing *decision* deterministically (no dependency on the
live agent's 2004 timing) by importing the exported helpers and exercising
them against stub terminal objects. It also pins the submit *ordering*
(#166): ``sendSubmit`` sends the bracketed block and the submitting CR as
two separate WS frames, so the CR can't be absorbed into paste finalization
instead of running the prompt. End-to-end delivery to the PTY is covered by
``test_paste_button.py`` / ``test_compose_bar.py``; lossless write delivery
by ``test_session_host_pty_realpty.py``; the actual on-device
byte-for-byte paste is the manual acceptance step.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.smoke

# Evaluate framePaste in the page against stub terminal objects, so the only
# variable is term.modes.bracketedPasteMode. framePaste is framing-only (no
# CR); the submitting CR ordering is exercised separately via sendSubmit,
# whose WS sends are captured into an ordered frame list by a stub `ws`.
_EVAL = r"""
async () => {
  const m = await import('/static/terminal.js');
  const on = { term: { modes: { bracketedPasteMode: true } } };
  const off = { term: { modes: { bracketedPasteMode: false } } };
  const bare = { };  // no term yet (WS open before agent boots)
  // Drive sendSubmit against a stub ws that records each input frame's data
  // in order — pins that the CR is its OWN frame, never glued to the block.
  function submitFrames(modes) {
    const frames = [];
    const t = {
      term: modes ? { modes } : undefined,
      ws: {
        readyState: WebSocket.OPEN,
        send: (d) => frames.push(JSON.parse(d).data),
      },
    };
    m.sendSubmit(t, 'hello world');
    return frames;
  }
  return {
    onPaste:   m.framePaste(on,  'hello world'),
    offPaste:  m.framePaste(off, 'hello world'),
    barePaste: m.framePaste(bare, 'hello world'),
    onSubmit:  submitFrames({ bracketedPasteMode: true }),
    offSubmit: submitFrames({ bracketedPasteMode: false }),
  };
}
"""


def test_frame_paste_brackets_only_when_mode_enabled(
    authed_page: Page, base_url: str
) -> None:
    authed_page.goto(base_url, wait_until="domcontentloaded")
    res = authed_page.evaluate(_EVAL)

    START, END = "\x1b[200~", "\x1b[201~"

    # Bracketed mode ON: payload wrapped, framing only — no CR appended.
    assert res["onPaste"] == f"{START}hello world{END}"

    # Bracketed mode OFF: never inject markers (they would land as literal
    # garbage in an agent that didn't ask for bracketed paste).
    assert res["offPaste"] == "hello world"

    # No term yet (WS open before the agent boots): treat as unframed.
    assert res["barePaste"] == "hello world"

    # Submit ordering (#166): the bracketed block goes in one frame and the
    # submitting CR follows as its OWN frame — never concatenated onto the
    # `\x1b[201~` end marker, where the TUI could absorb it into paste
    # finalization instead of running the prompt.
    assert res["onSubmit"] == [f"{START}hello world{END}", "\r"]

    # With bracketed mode off there's no end marker, but the CR is still a
    # separate, final frame so the path stays uniform.
    assert res["offSubmit"] == ["hello world", "\r"]
