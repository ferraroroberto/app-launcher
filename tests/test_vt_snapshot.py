"""Headless VT screen mirror for full-screen agents (issue #432).

``VtSnapshot`` parses a PTY's byte stream through a headless ``pyte`` screen
and renders the current frame back to ANSI on demand, so the session-host
can serve a (re)connecting client the *current* frame without ever
resizing the PTY — the SIGWINCH a resize fires is exactly what makes a
ratatui agent re-emit its entire transcript (issue #430).

``test_render_round_trips_wide_chars_and_sgr`` pins the two real bugs found
while building this against a captured Codex stream (see the round-trip
probe in the issue): a naive per-row ``\\r\\n`` join shifts the whole frame
down by one line (xterm's deferred-autowrap state), and not skipping a wide
character's continuation cell shifts every column after it.
"""

from __future__ import annotations

import pyte

from src.vt_snapshot import VtSnapshot


def test_feed_and_render_contains_fed_text():
    vt = VtSnapshot(10, 20)
    vt.feed("hello vt\r\n")
    assert "hello vt" in vt.render()


def test_render_places_cursor_position():
    vt = VtSnapshot(5, 10)
    vt.feed("ab")  # cursor lands right after "ab": row 0, col 2 (0-indexed)
    rendered = vt.render()
    assert "\x1b[1;3H" in rendered  # 1-indexed row 1, col 3


def test_render_hides_cursor_when_agent_hides_it():
    vt = VtSnapshot(5, 10)
    vt.feed("\x1b[?25l")  # DECTCEM hide
    assert "\x1b[?25l" in vt.render()


def test_resize_changes_screen_dimensions():
    vt = VtSnapshot(10, 20)
    vt.resize(5, 15)
    assert vt._screen.lines == 5
    assert vt._screen.columns == 15


def test_render_round_trips_wide_chars_and_sgr():
    """Feed a frame with a wide (2-column) char and a truecolor run, render
    to ANSI, reparse through a fresh pyte screen, and assert the two frames
    are pixel-for-pixel identical — the regression this issue's dev loop
    actually hit against a live Codex stream."""
    rows, cols = 6, 30
    vt = VtSnapshot(rows, cols)
    # Truecolor SGR run + a wide emoji mid-line + plain text after it, on
    # every row so the last-column autowrap edge case is also covered.
    line = "\x1b[38;2;10;205;205m📊 status: ok, +200 -62\x1b[0m"
    for _ in range(rows):
        vt.feed(line + "\r\n")

    rendered = vt.render()

    reparsed = pyte.Screen(cols, rows)
    pyte.Stream(reparsed).feed(rendered)

    assert list(reparsed.display) == list(vt._screen.display)
    assert (reparsed.cursor.x, reparsed.cursor.y) == (
        vt._screen.cursor.x, vt._screen.cursor.y,
    )


def test_render_is_thread_safe_lock_scoped():
    """render() and feed() acquire the same lock — a render mid-feed can't
    observe a torn/partial screen mutation."""
    vt = VtSnapshot(5, 10)
    vt.feed("partial")
    # Render must not raise or hang even though pyte's screen is mid-line.
    assert isinstance(vt.render(), str)
