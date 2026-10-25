"""A steer to a busy agent is submitted, not stranded — issue #1548.

Two steers of ~800 bytes, several paragraphs each, sent through ``/input`` to
two working Claude sessions, were found minutes later sitting unsent on their
input lines. One watcher had reported ``defer_vanished`` after ~113 s with the
text still there; the other was still ``pending``, waiting for a quiet window
that a working agent does not give.

These tests drive the session's real reader thread with a scripted PTY (the
harness from ``test_bg_agents_submit``), so turn state and the
composer are read off the stream the way the session-host reads them:

- a type-ahead agent mid-turn gets Enter at once, and the queued-message
  markers Claude Code paints confirm it;
- a composer no keystroke has touched still holds the payload, even when the
  differential renderer never paints it again;
- a watcher that fails still ends on a verdict.
"""

from __future__ import annotations

import pytest

from src import session_host_input
from src.session_host import PtySession
from src.session_hosting import deferred_submit
from src.session_host_input import (
    INPUT_DEFER_FAILED,
    INPUT_DEFER_UNCLEAR,
    INPUT_DEFER_VANISHED,
    INPUT_DEFERRED,
    INPUT_OK,
    INPUT_UNVERIFIED,
    touches_composer,
)
from tests.unit.session_host.test_bg_agents_submit import (  # noqa: F401 — fixtures
    _SPINNER_FRAME,
    _TICK_S,
    _TIMER_TICK_FRAME,
    _TURN_START_FRAME,
    _ScriptedPty,
    scripted,
)

# An invented steer of the stranded size: 800+ bytes over several paragraphs.
_STEER = "\n\n".join(
    [
        "CHIEF - change of plan for the harbour report before you open the PR.",
        "Drop the tide-table section entirely; the numbers in it came from the "
        "old sample file and nobody has checked them against the new one yet.",
        "Keep the ferry timetable, but move it below the weather summary so the "
        "page reads in the order people actually use it on the quay.",
        "When the gate is green, push the branch and stop there. Do not merge; "
        "I want to read the diff on my phone first and I will say when.",
        "If anything in this conflicts with what the issue says, the issue wins "
        "and you tell me which part, in one line, before doing anything else.",
        "One more thing: the screenshots in the old draft showed the morning "
        "ferry twice. Regenerate them from the new sample file, check each one "
        "by eye against the timetable, and leave the old ones out of the diff.",
    ]
)
assert len(_STEER.encode()) > 800 and _STEER.count("\n\n") >= 4

# Claude Code collapsing that paste into a chip, painted word by word with an
# SGR reset between the words, as a live session paints it (#1109).
_CHIP_FRAME = (
    "\x1b[2K\x1b[0;4m❯\xa0[Pasted\x1b[0m \x1b[0;4mtext\x1b[0m "
    "\x1b[0;4m#3 +8 lines]\x1b[0m"
)
# What Claude Code v2.1.296 painted once a mid-turn Enter queued the message
# (captured from the #1548 probe): the queued text's hint, then the composer.
_QUEUED_FRAME = (
    "\n\n\x1b[3G\x1b[38;2;153;153;153mctrl+x\x1b[10Gctrl+s\x1b[17Gto\x1b[20Gsend"
    "\x1b[25Gnow\x1b[39m\n\n\n\n\x1b[38;2;153;153;153m❯\xa0\x1b[39m\x1b[2mPress"
    "\x1b[9Gup\x1b[12Gto\x1b[15Gedit\x1b[20Gqueued\x1b[27Gmessages\x1b[22m\n\n"
)
_DIALOG_FRAME = "\r\n Do you want to proceed?\r\n ❯ 1. Yes\r\n   2. No\r\n"


def _defer_steer(session, pty, clock, monkeypatch, first_paint, on_tick) -> tuple:
    """Send ``_STEER`` through ``submit_input``; return the watcher's args.

    The composer paints ``first_paint`` on the first poll, then ``on_tick``
    paints whatever the screen does every ``_TICK_S`` — never quiet for the
    paste-settle, so the submit is handed to the watcher.
    """
    armed: list = []
    monkeypatch.setattr(
        PtySession, "_arm_deferred_submit", lambda self, *a, **kw: armed.append((a, kw))
    )
    state = {"painted": False, "last_tick": clock.now, "n": 0}

    def _on_sleep(c) -> None:
        if not state["painted"]:
            state["painted"] = True
            pty.paint(first_paint)
        if c.now - state["last_tick"] >= _TICK_S:
            state["last_tick"] = c.now
            state["n"] += 1
            on_tick(state["n"])

    clock.on_sleep(_on_sleep)
    outcome = session.submit_input(_STEER, True)
    assert outcome.reason == INPUT_DEFERRED
    assert outcome.submit_state == "pending"
    assert pty.crs == 0
    assert len(armed) == 1
    return armed[0]


def _working(pty: _ScriptedPty, queue_on_enter: bool = True):
    """A main turn running: the spinner repaints every tick. Once an Enter
    is in, Claude Code shows the message queued (if ``queue_on_enter``)."""
    state = {"queued": False}

    def _tick(_n: int) -> None:
        pty.paint(_SPINNER_FRAME)
        if queue_on_enter and pty.crs and not state["queued"]:
            state["queued"] = True
            pty.paint(_QUEUED_FRAME)

    return _tick


# ------------------------------------------------ type-ahead mid-turn (#1548)


def test_a_long_steer_to_a_working_claude_is_queued_and_confirmed(scripted, monkeypatch):
    """The headline: Enter goes in while the agent works, and the outcome is
    a confirmed ``ok`` that says the agent queued it.

    On the #763 rule the watcher waited for a quiet window that never came,
    and the steer sat in the composer until a human pressed Enter.
    """
    session, pty, clock = scripted
    pty.paint(_TURN_START_FRAME)

    args, kw = _defer_steer(session, pty, clock, monkeypatch, _CHIP_FRAME, _working(pty))
    session._run_deferred_submit(*args, **kw)

    assert pty.crs == 1
    assert session.last_input["reason"] == INPUT_OK
    assert session.last_input["queued"] is True
    assert session.last_input["submit_state"] == "confirmed"
    assert session.last_input["delivered"] is True
    assert session.last_input["waited_ms"] < 2_000
    # One bare CR after the paste — the text is never sent twice.
    assert pty.writes[-1] == "\r"
    assert "".join(pty.writes[:-1]) == "\x1b[200~" + _STEER + "\x1b[201~"


def test_a_mid_turn_enter_nothing_shows_queued_is_unconfirmed(scripted, monkeypatch):
    """An Enter the agent absorbed would look like this: nothing queued. The
    outcome says the submit is unconfirmed, never ``ok``, and the watcher
    does not press again."""
    session, pty, clock = scripted
    pty.paint(_TURN_START_FRAME)

    args, kw = _defer_steer(
        session, pty, clock, monkeypatch, _CHIP_FRAME, _working(pty, queue_on_enter=False)
    )
    session._run_deferred_submit(*args, **kw)

    assert pty.crs == 1
    assert session.last_input["reason"] == INPUT_UNVERIFIED
    assert session.last_input["submit_confirmed"] is None
    assert session.last_input["submit_state"] == "unconfirmed"
    assert session.last_input["queued"] is False


def test_a_dialog_on_screen_mid_turn_gets_no_enter(scripted, monkeypatch):
    """Type-ahead does not lift the dialog guard: a CR into a permission
    prompt picks an option, so nothing is written."""
    session, pty, clock = scripted
    pty.paint(_TURN_START_FRAME)

    args, kw = _defer_steer(
        session, pty, clock, monkeypatch, _CHIP_FRAME + _DIALOG_FRAME, _working(pty)
    )
    session._run_deferred_submit(*args, **kw)

    assert pty.crs == 0
    assert session.last_input["reason"] == INPUT_DEFER_UNCLEAR
    assert session.last_input["submit_state"] == "not_submitted"


# --------------------------------- a composer the renderer never repaints


def _status_row_ticks_for(pty: _ScriptedPty, clock, seconds: float, extra=None):
    """A status row ticking with no spinner — not a main turn, and not quiet
    — for ``seconds``, then silence. The composer is never painted again,
    which is what Claude Code's differential renderer does to an unchanged
    input line.

    ``extra(n)`` runs on the watcher's ticks only, numbered from 1: keyboard
    writes during ``submit_input``'s own wait would queue behind its write
    lock, as they do live. Set ``_tick.watching`` once the watcher runs.
    """
    start = clock.now
    watched = {"n": 0}

    def _tick(n: int) -> None:
        if clock.now - start < seconds:
            pty.paint(_TIMER_TICK_FRAME.format(digit=n % 10))
            if extra is not None and _tick.watching:
                watched["n"] += 1
                extra(watched["n"])

    _tick.watching = False
    return _tick


def test_a_steer_the_renderer_never_repaints_is_still_submitted(scripted, monkeypatch):
    """Session A's shape: ~113 s of a ticking status row, then quiet, with the
    chip painted once at ingest and never again. On the frame-lookback rule
    that read as ``defer_vanished`` and nothing was pressed, with the steer
    still on the input line. Nobody touched the composer, so it is still
    there: Enter goes in."""
    session, pty, clock = scripted
    # The evidence's own 113 s, past the harness's shortened give-up.
    monkeypatch.setattr(deferred_submit, "_DEFER_CAP_MS", 300_000)

    args, kw = _defer_steer(
        session, pty, clock, monkeypatch, _CHIP_FRAME, _status_row_ticks_for(pty, clock, 113)
    )
    session._run_deferred_submit(*args, **kw)

    assert pty.crs == 1
    assert session.last_input["reason"] == INPUT_OK
    assert session.last_input["submit_state"] == "confirmed"
    assert session.last_input["queued"] is False
    assert session.last_input["waited_ms"] >= 110_000


def test_terminal_replies_do_not_count_as_touching_the_composer(scripted, monkeypatch):
    """An attached xterm answers queries and reports focus without anyone
    typing; none of that can move the payload, so the steer still lands."""
    session, pty, clock = scripted
    replies = ["\x1b[I", "\x1b[O", "\x1b[?1;2c", "\x1b[12;1R", "\x1b]11;rgb:0000/0000/0000\x1b\\"]

    def _reply(n: int) -> None:
        if n <= len(replies):
            session.write(replies[n - 1])

    tick = _status_row_ticks_for(pty, clock, 20, extra=_reply)
    args, kw = _defer_steer(session, pty, clock, monkeypatch, _CHIP_FRAME, tick)
    tick.watching = True
    session._run_deferred_submit(*args, **kw)

    assert session._composer_touches == 0
    assert pty.crs == 1
    assert session.last_input["reason"] == INPUT_OK


def test_an_enter_typed_meanwhile_means_the_steer_is_gone(scripted, monkeypatch):
    """Somebody pressed Enter at the keyboard while the watcher waited, and
    the payload is not on screen: it went with that Enter. The watcher reports
    ``defer_vanished`` and presses nothing — the only CR is the human's."""
    session, pty, clock = scripted

    def _human_enter(n: int) -> None:
        if n == 4:
            session.write("\r")

    tick = _status_row_ticks_for(pty, clock, 20, extra=_human_enter)
    args, kw = _defer_steer(session, pty, clock, monkeypatch, _CHIP_FRAME, tick)
    tick.watching = True
    session._run_deferred_submit(*args, **kw)

    assert session._composer_touches == 1
    assert pty.crs == 1
    assert session.last_input["reason"] == INPUT_DEFER_VANISHED


@pytest.mark.parametrize(
    "data, expected",
    [
        pytest.param("hello there", False, id="typed-text"),
        pytest.param("\x1b[I", False, id="focus-in"),
        pytest.param("\x1b[O", False, id="focus-out"),
        pytest.param("\x1b[?1;2c", False, id="device-attributes"),
        pytest.param("\x1b[>0;276;0c", False, id="secondary-device-attributes"),
        pytest.param("\x1b[24;80R", False, id="cursor-position"),
        pytest.param("\x1b[?2026;2$y", False, id="mode-report"),
        pytest.param("\x1b]11;rgb:1e1e/1e1e/1e1e\x1b\\", False, id="osc-colour-reply"),
        pytest.param("\r", True, id="enter"),
        pytest.param("\x7f", True, id="backspace"),
        pytest.param("\x03", True, id="ctrl-c"),
        pytest.param("\x1b", True, id="escape"),
        pytest.param("\x1b[A", True, id="arrow-up-history"),
        pytest.param("\x1b[200~more\x1b[201~", True, id="paste"),
        pytest.param("text then \r", True, id="typed-and-submitted"),
    ],
)
def test_which_keyboard_writes_touch_the_composer(data, expected):
    assert touches_composer(data) is expected


# ------------------------------------------------ every watcher ends (#1548)


def test_a_watcher_that_fails_still_records_a_verdict(scripted, monkeypatch):
    """A watcher that raised used to log at debug and leave ``last_input`` at
    ``pending`` for good — indistinguishable from one still waiting. It now
    records its own failure, with no CR written."""
    session, pty, clock = scripted

    args, kw = _defer_steer(
        session, pty, clock, monkeypatch, _CHIP_FRAME, _status_row_ticks_for(pty, clock, 3)
    )

    def _boom(self, mark):
        raise RuntimeError("ring read failed")

    monkeypatch.setattr(PtySession, "_normalized_since", _boom)
    session._run_deferred_submit(*args, **kw)

    assert pty.crs == 0
    assert session.last_input["reason"] == INPUT_DEFER_FAILED
    assert session.last_input["submit_state"] == "not_submitted"
    assert session.last_input["delivered"] is False


def test_a_superseded_watcher_leaves_the_newer_verdict(scripted, monkeypatch):
    """Supersession is the one watcher exit with no verdict of its own: the
    newer write's verdict is the one on record, and it stays there."""
    session, pty, clock = scripted
    newer = {"reason": "newer-write"}

    def _supersede(n: int) -> None:
        if n == 2:
            # What submit_input does for the newer call.
            session._defer_seq += 1
            session.last_input = newer

    tick = _status_row_ticks_for(pty, clock, 20, extra=_supersede)
    args, kw = _defer_steer(session, pty, clock, monkeypatch, _CHIP_FRAME, tick)
    tick.watching = True
    session._run_deferred_submit(*args, **kw)

    assert pty.crs == 0
    assert session.last_input is newer


def test_queued_markers_survive_normalization():
    normalized = session_host_input._normalize_echo(_QUEUED_FRAME)
    assert all(marker in normalized for marker in deferred_submit._QUEUED_MARKERS)
