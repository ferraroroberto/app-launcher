"""Turn a rendered VT frame into the text a human would read off the screen.

The real-agent PTY probes (``test_claude_pty_submit``, ``test_claude_pty_esc``,
``test_codex_pty_submit``) assert that a marker "is in the composer" by
substring-matching ``PtySession.snapshot_frame()``.  That frame is the *raw*
VT stream, escape sequences included, so a substring only survives for as long
as the TUI happens to paint it as one unstyled run.  Match rendered text.

What bit (#1109): Claude Code v2.1.278 paints the collapsed paste chip one word
at a time with an SGR reset between the words, so the frame holds

    '\x1b[0;4m\u276f\xa0[Pasted\x1b[0m \x1b[0;4mtext\x1b[0m \x1b[0;4m#1]\x1b[0m'

The screen reads ``\u276f [Pasted text #1]`` and the composer is holding the paste
exactly as the probe intends, but the literal substring ``[Pasted text`` exists
nowhere in that string.  Note also the non-breaking space after the prompt
glyph: anything matching on ``"\u276f "`` with an ordinary space will appear to
work and then not, so ``rendered()`` normalises it and no caller has to know.
"""

from __future__ import annotations

import re

# ``VtSnapshot.render()`` paints the live frame with one absolute
# ``ESC[<row>;1H`` per row, after the plain-text scrollback history (#432).
FRAME_ROW = re.compile(r"\x1b\[(\d+);1H")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def rendered(text: str) -> str:
    """``text`` as it appears on screen: no escapes, no non-breaking spaces.

    Row-position escapes become newlines rather than vanishing, so two rows
    closing up can never manufacture a match across the boundary between them.
    """
    return ANSI.sub("", FRAME_ROW.sub("\n", text)).replace("\xa0", " ")
