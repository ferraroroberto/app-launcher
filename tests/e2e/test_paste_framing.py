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
live agent's 2004 timing) by importing the exported helper and exercising
the four cases. End-to-end delivery to the PTY is covered by
``test_paste_button.py`` / ``test_compose_bar.py``; lossless write delivery
by ``test_session_host_pty_realpty.py``; the actual on-device
byte-for-byte paste is the manual acceptance step.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.smoke

# Evaluate framePaste in the page against stub terminal objects, so the only
# variable is term.modes.bracketedPasteMode. Returns the four framings.
_EVAL = r"""
async () => {
  const m = await import('/static/terminal.js');
  const on = { term: { modes: { bracketedPasteMode: true } } };
  const off = { term: { modes: { bracketedPasteMode: false } } };
  const bare = { };  // no term yet (WS open before agent boots)
  return {
    onPaste:    m.framePaste(on,  'hello world', false),
    onSubmit:   m.framePaste(on,  'hello world', true),
    offPaste:   m.framePaste(off, 'hello world', false),
    offSubmit:  m.framePaste(off, 'hello world', true),
    barePaste:  m.framePaste(bare, 'hello world', false),
  };
}
"""


def test_frame_paste_brackets_only_when_mode_enabled(
    authed_page: Page, base_url: str
) -> None:
    authed_page.goto(base_url, wait_until="domcontentloaded")
    res = authed_page.evaluate(_EVAL)

    START, END = "\x1b[200~", "\x1b[201~"

    # Bracketed mode ON: payload wrapped; a submit CR sits OUTSIDE the end
    # marker (inside it would be literal pasted text and never submit).
    assert res["onPaste"] == f"{START}hello world{END}"
    assert res["onSubmit"] == f"{START}hello world{END}\r"

    # Bracketed mode OFF: never inject markers (they would land as literal
    # garbage in an agent that didn't ask for bracketed paste).
    assert res["offPaste"] == "hello world"
    assert res["offSubmit"] == "hello world\r"

    # No term yet (WS open before the agent boots): treat as unframed.
    assert res["barePaste"] == "hello world"
