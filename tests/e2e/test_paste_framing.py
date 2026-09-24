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

It also pins the attachment settle (#1211): a send carrying an attached
image path holds the CR while Claude Code is still converting the path
(its "Pasting…" hint painted, the "[Image #N]" chip not yet), however long
that takes, where #450's fixed 350 ms defer let a large photo's conversion
swallow the CR and strand the prompt unsent.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

pytestmark = [pytest.mark.smoke, pytest.mark.usefixtures("chromium_projection_only")]

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
  // #1211: an image send against a stub terminal whose output is stamped
  // the way terminal-connection.js stamps real frames: "Pasting…" at once,
  // the chip only after the old 350 ms defer has long run out. The CR must
  // wait for the chip, then follow it.
  const compose = await import('/static/terminal-compose.js');
  const conn = await import('/static/terminal-connection.js');
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const text = 'describe it\n\nC:\\p\\.launcher-tmp\\image.png';
  const attach = [];
  const t = { ws: { readyState: WebSocket.OPEN,
                    send: (d) => attach.push(JSON.parse(d).data) } };
  const t0 = Date.now();
  m.sendSubmit(t, text, compose.submitOptions(text, { hasImage: true }));
  await sleep(20);
  conn.stampOutput(t, '\x1b[38;2;153;153;153mPasting…\x1b[39m');
  await sleep(700);
  const whileConverting = attach.slice();
  conn.stampOutput(t, '\x1b[3A[Image\x1b[10G#1]describe it');
  const chipAt = Date.now() - t0;
  while (attach.length < 2 && Date.now() - t0 < 5000) await sleep(20);
  return {
    onPaste:   m.framePaste(on,  'hello world'),
    offPaste:  m.framePaste(off, 'hello world'),
    barePaste: m.framePaste(bare, 'hello world'),
    onSubmit:  submitFrames({ bracketedPasteMode: true }),
    offSubmit: submitFrames({ bracketedPasteMode: false }),
    attach: { text, whileConverting, frames: attach, chipAt,
              crAt: attach.length > 1 ? Date.now() - t0 : null },
    shortOpts: compose.submitOptions('hi', { hasImage: false }) || null,
    bulkOpts: compose.submitOptions('x'.repeat(600), { hasImage: false }),
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

    # #1211: nothing but the text went out while the agent was converting
    # the image, 700 ms in, twice the old fixed defer; the CR followed the
    # chip, as its own frame, once the stream went quiet after it.
    attach = res["attach"]
    assert attach["whileConverting"] == [attach["text"]], attach
    assert attach["frames"] == [attach["text"], "\r"], attach
    assert attach["crAt"] >= attach["chipAt"], attach

    # A short plain send stays instant; bulk text keeps #499's watch.
    assert res["shortOpts"] is None
    assert res["bulkOpts"] == {"bulkSettle": True}
