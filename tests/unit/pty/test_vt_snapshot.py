"""Headless VT screen mirror for full-screen agents (issues #432, #435).

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

``test_render_prepends_bounded_scrollback_history`` and friends pin issue
#435: a plain ``pyte.Screen`` only ever knows the current frame, so a cold
reconnect landed on exactly one screen with nothing to scroll into.
``VtSnapshot`` now uses ``pyte.HistoryScreen`` and prepends a bounded window
of real scrollback (plain scrolling text, not absolute-positioned) ahead of
the current frame.
"""

from __future__ import annotations

import pyte

from src.vt_snapshot import _HISTORY_LINES, VtSnapshot


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


def test_render_has_no_history_prefix_when_nothing_scrolled():
    """Content that fits entirely within the screen never scrolls off the
    top, so history stays empty and render() is unchanged from #432 —
    the backward-compatible case every earlier test already exercises."""
    vt = VtSnapshot(10, 20)
    vt.feed("only line\r\n")
    assert len(vt._screen.history.top) == 0
    # No stray blank scrollback line prepended before the frame content.
    assert vt.render().startswith("\x1b[1;1H")


def test_render_prepends_scrolled_off_lines_as_history():
    """Lines pushed off the top by natural scrolling land in
    ``history.top`` and are prepended to render() as plain scrolling
    text, ahead of the current frame — so a cold-reconnecting client's
    own xterm accumulates them into its own scrollback."""
    rows, cols = 5, 20
    vt = VtSnapshot(rows, cols)
    for i in range(rows * 3):  # well beyond one screenful
        vt.feed(f"line {i}\r\n")

    assert len(vt._screen.history.top) > 0
    rendered = vt.render()
    # An early, long-scrolled-off line is present as history text...
    assert "line 0" in rendered
    # ...strictly before the current frame's absolute-positioned content.
    frame_start = rendered.index("\x1b[1;1H")
    assert rendered.index("line 0") < frame_start


def test_render_history_preserves_chronological_order():
    rows, cols = 5, 20
    vt = VtSnapshot(rows, cols)
    for i in range(rows * 4):
        vt.feed(f"line {i}\r\n")

    rendered = vt.render()
    # Two lines that both survived into history must still appear oldest
    # first — reversing the order would scramble the replayed transcript.
    # (Rows are space-padded to the full column width, so match the text
    # only — not a specific trailing terminator.)
    early = rendered.index("line 1")
    later = rendered.index("line 2")
    assert early < later


def test_history_is_capped_at_history_lines():
    rows, cols = 5, 20
    vt = VtSnapshot(rows, cols)
    for i in range(_HISTORY_LINES * 2):
        vt.feed(f"line {i}\r\n")

    assert len(vt._screen.history.top) <= _HISTORY_LINES


def test_render_with_history_still_lands_current_frame_correctly():
    """The regression this issue's dev loop actually hit against a real
    long Codex stream: prepending history text must not shift or corrupt
    the current frame — reparsing render() output must reproduce the
    exact same visible frame as the source screen."""
    rows, cols = 6, 30
    vt = VtSnapshot(rows, cols)
    for i in range(rows * 5):
        vt.feed(f"\x1b[38;2;10;205;205mrow {i} 📊 ok\x1b[0m\r\n")

    rendered = vt.render()
    reparsed = pyte.Screen(cols, rows)
    pyte.Stream(reparsed).feed(rendered)

    assert list(reparsed.display) == list(vt._screen.display)
    assert (reparsed.cursor.x, reparsed.cursor.y) == (
        vt._screen.cursor.x, vt._screen.cursor.y,
    )


def test_render_does_not_swallow_the_seam_between_history_and_frame():
    """The real bug found live (issue #435 follow-up, reported as
    "conversation beginning visible, a chunk in the middle missing, latest
    lines visible" during an actively-growing Codex session): after the
    history text scrolls naturally, the client's viewport still holds the
    LAST `rows` history lines — the frame then addresses that same
    viewport with absolute cursor positions, silently overwriting them IN
    PLACE instead of ever letting them scroll into real scrollback. A
    plain, oversized reparse buffer hides this (nothing needs to scroll,
    so nothing gets clobbered) — the bug only shows up against a
    viewport-sized reparse target that models a real terminal, which is
    exactly what a live xterm.js instance is.
    """
    rows, cols = 10, 20
    vt = VtSnapshot(rows, cols)
    total = 50
    for i in range(total):
        vt.feed(f"line {i:03d}\r\n")

    rendered = vt.render()
    reparsed = pyte.HistoryScreen(cols, rows, history=1000)
    pyte.Stream(reparsed).feed(rendered)

    def row_text(row):
        return "".join(row[x].data or " " for x in range(cols)).strip()

    seen = [row_text(r) for r in reparsed.history.top]
    seen += [line.strip() for line in reparsed.display]
    numbers = sorted(
        int(s.split()[1]) for s in seen if s.startswith("line ")
    )
    missing = [n for n in range(total) if n not in numbers]
    assert missing == [], (
        f"line(s) {missing} vanished at the history/frame seam — the "
        "frame's absolute positioning overwrote real conversation content "
        "before it ever reached scrollback"
    )
    assert numbers == sorted(set(numbers)), (
        "a line number appeared twice — history and frame overlapped "
        "instead of being contiguous"
    )


def test_render_is_thread_safe_lock_scoped():
    """render() and feed() acquire the same lock — a render mid-feed can't
    observe a torn/partial screen mutation."""
    vt = VtSnapshot(5, 10)
    vt.feed("partial")
    # Render must not raise or hang even though pyte's screen is mid-line.
    assert isinstance(vt.render(), str)


def test_feed_survives_private_device_status_query(caplog):
    """Issue #711: pyte dispatches any private CSI as
    ``csi_dispatch[char](*params, private=True)``, but upstream
    ``Screen.report_device_status`` doesn't accept that keyword and raises
    ``TypeError`` — the empirically-reproduced trigger being Copilot CLI
    1.0.77's startup ``ESC[?996n`` light/dark colour-scheme query. Feeding
    this used to raise straight out of ``feed()``, which on the real
    reader thread (``session_host.py::PtySession._read_loop``) killed the
    thread silently: the session kept reporting alive and input kept
    working, but scrollback/transcript/every subscriber went dark forever.
    ``feed()`` must swallow the parser error and keep mirroring."""
    vt = VtSnapshot(5, 20)
    vt.feed("before ")
    vt.feed("\x1b[?996n")  # private DSR — must not raise
    vt.feed("after\r\n")
    assert "after" in vt.render()


def test_feed_swallows_unexpected_parser_error_and_logs_breadcrumb(caplog):
    """The private-DSR trigger above is now handled cleanly (no exception at
    all) by ``_MutedQueryScreen`` — this pins the *generic* safety net in
    ``feed()`` itself, for whatever pyte parser error isn't specifically
    muted. A real reader thread must never die from this; a breadcrumb is
    the only observable trace."""
    import logging

    vt = VtSnapshot(5, 20)
    boom = RuntimeError("simulated pyte parser error")
    vt._stream.feed = lambda chunk: (_ for _ in ()).throw(boom)

    with caplog.at_level(logging.WARNING, logger="src.vt_snapshot"):
        vt.feed("anything")  # must not raise

    assert any("pyte parser error" in r.message for r in caplog.records)


def test_feed_non_private_device_status_still_delegates():
    """A non-private DSR (mode without ``private=True``) is not the bug
    this issue is about — pyte's own default handling still applies, and
    must not be swallowed by the private-only mute."""
    vt = VtSnapshot(5, 20)
    vt.feed("\x1b[5n")  # DSR mode 5, not private — pyte's default is a no-op
    vt.feed("still works\r\n")
    assert "still works" in vt.render()
