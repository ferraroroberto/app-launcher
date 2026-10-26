"""Unit pin for the frame renderer the real-agent PTY probes match against.

The probes that exercise ``rendered()`` for real — ``test_claude_pty_submit``,
``test_claude_pty_esc``, ``test_codex_pty_submit`` — all skip wherever the
agent CLI is absent, CI included, so without this module nothing anywhere
pins the matching rule that #1109 turned out to depend on.  These cases run
everywhere and need no agent.

The frame below is the real one: captured verbatim from a live Claude Code
v2.1.278 ConPTY at +15 s after the bracketed dictation-sized paste that
``test_claude_pty_submit`` sends.
"""

from __future__ import annotations

from tests._pty_frame_text import rendered

# Row 10 of that live frame, holding the collapsed paste chip.  Claude Code
# paints it one word at a time with an SGR reset between the words, and
# separates the prompt glyph from it with a non-breaking space.
_CHIP_ROW = (
    "\x1b[10;1H\x1b[0;4m❯\xa0[Pasted\x1b[0m \x1b[0;4mtext\x1b[0m "
    "\x1b[0;4m#1]\x1b[0m "
)


def test_sgr_split_paste_chip_is_matchable_only_once_rendered() -> None:
    """The #1109 defect itself: on screen, but not in the raw stream."""
    assert "[Pasted text" not in _CHIP_ROW
    assert "[Pasted text" in rendered(_CHIP_ROW)


def test_non_breaking_space_after_the_prompt_glyph_is_normalised() -> None:
    """A matcher written with an ordinary space must not silently miss."""
    assert "❯ " not in _CHIP_ROW
    assert "❯ [Pasted text #1]" in rendered(_CHIP_ROW)


def test_two_rows_never_close_up_into_a_false_match() -> None:
    """Stripping escapes must not manufacture a marker across a row break.

    A matcher loose enough to join rows would report a paste the composer is
    not holding — worse than the red this replaces, which at least told the
    truth.  Row-position escapes become newlines, so the join cannot happen.

    The row below carries the trailing space a real frame is padded with,
    which is what makes the join dangerous: drop the newline and the two rows
    read as ``[Pasted text #1]``, a clean false match.
    """
    frame = "\x1b[1;1H\x1b[0;4m[Pasted \x1b[0m\x1b[2;1Htext #1]"
    assert "[Pasted text" not in rendered(frame)
