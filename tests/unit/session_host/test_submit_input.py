"""PtySession.submit_input: bracketed-paste framing, the bulk settle and the deferred submit watcher, against a fake PTY and clock."""

from __future__ import annotations


import pytest

from src.session_host_input import (
    _BULK_CAP_MS,
    _BULK_FLOOR_MS,
    _BULK_QUIET_MS,
    _BULK_SUBMIT_THRESHOLD_CHARS,
    _INGEST_CAP_MS,
    INPUT_DEFER_TIMEOUT,
    INPUT_DEFER_UNCLEAR,
    INPUT_DEFER_VANISHED,
    INPUT_DEFERRED,
    INPUT_DROPPED,
    INPUT_NOOP,
    INPUT_NOT_INGESTED,
    INPUT_OK,
    INPUT_UNVERIFIED,
)
from src import session_host_input
from src.session_hosting.deferred_submit import _DEFER_CAP_MS, _DEFER_QUIET_MS

from tests.unit.session_host._support import _FakeClock, _echo, _make_session


# --------------------------------------------------------------- framing


def test_short_payload_is_bracketed_when_paste_mode_on(clock):
    session = _make_session()
    session._bracketed_paste_mode = True

    session.submit_input("hi", True)

    calls = [c.args[0] for c in session._pty.write.call_args_list]
    assert calls[0] == "\x1b[200~hi\x1b[201~"
    assert calls[1] == "\r"


def test_payload_not_bracketed_when_paste_mode_off(clock):
    """framePaste's own gate (#611): a literal \\x1b[200~ sent to an agent
    that never announced bracketed-paste support is garbage, not a paste —
    so bracketing only happens once DECSET 2004 has actually been observed."""
    session = _make_session()
    assert session._bracketed_paste_mode is False  # default, nothing observed yet

    session.submit_input("hi", True)

    calls = [c.args[0] for c in session._pty.write.call_args_list]
    assert calls[0] == "hi"
    assert calls[1] == "\r"


# ---------------------------------------------------------- short = instant


def test_short_payload_submits_with_no_wait(clock):
    session = _make_session()
    session._bracketed_paste_mode = True

    outcome = session.submit_input("hello", True)

    assert outcome.delivered is True
    assert outcome.submitted is True
    # Nothing was observed, so nothing is claimed: a short send is reported
    # as unverified, never as a confirmed submit (#760).
    assert outcome.reason == INPUT_UNVERIFIED
    assert outcome.submit_confirmed is None
    assert clock.sleep_calls == []  # no settle wait for a short payload
    assert session._pty.write.call_count == 2


def test_no_submit_skips_the_cr(clock):
    session = _make_session()

    session.submit_input("draft", False)

    assert session._pty.write.call_count == 1
    session._pty.write.assert_called_once_with("draft")


# --------------------------------------------------------------- bare submit


def test_bare_submit_with_no_data_writes_only_cr(clock):
    session = _make_session()
    session._bracketed_paste_mode = True

    outcome = session.submit_input("", True)

    assert outcome.delivered is True
    assert outcome.submitted is True
    assert outcome.reason == INPUT_UNVERIFIED  # no payload to verify against
    session._pty.write.assert_called_once_with("\r")


def test_blank_data_without_submit_is_a_true_noop(clock):
    session = _make_session()

    outcome = session.submit_input("", False)

    assert outcome.delivered is True
    assert outcome.reason == INPUT_NOOP
    session._pty.write.assert_not_called()


# ------------------------------------------------------------------- bulk


def test_bulk_payload_waits_for_echo_then_quiet_before_submitting(clock):
    """#499's echo-then-quiet protocol: the CR is held until output arrives
    after the send AND has been silent for _BULK_QUIET_MS, not just a fixed
    delay."""
    session = _make_session()
    payload = "steer " * 120  # past the bulk threshold, with real characters

    # Simulate the reader thread: echo arrives once the floor has passed,
    # then goes quiet. Scripted via the fake clock's sleep callback.
    state = {"echoed": False}
    sent_at_holder = {"t": clock.now}

    def _on_sleep(c: _FakeClock) -> None:
        elapsed_ms = (c.now - sent_at_holder["t"]) * 1000
        if not state["echoed"] and elapsed_ms >= _BULK_FLOOR_MS:
            _echo(session, payload)
            session._last_output_at = c.now
            state["echoed"] = True

    clock.on_sleep(_on_sleep)

    outcome = session.submit_input(payload, True)

    assert outcome.delivered is True
    assert outcome.reason == INPUT_OK
    assert outcome.ingested is True
    assert outcome.submit_confirmed is True
    assert len(clock.sleep_calls) > 0  # it actually waited, not instant
    calls = [c.args[0] for c in session._pty.write.call_args_list]
    assert calls[-1] == "\r"
    # The CR must not have been sent before floor + quiet elapsed, and must
    # not have run all the way out to the cap either (it actually settled).
    total_waited_ms = sum(clock.sleep_calls) * 1000
    _poll_tolerance_ms = 100  # a couple of poll intervals' slack
    assert total_waited_ms >= _BULK_FLOOR_MS + _BULK_QUIET_MS - _poll_tolerance_ms
    assert total_waited_ms < _BULK_CAP_MS
    assert total_waited_ms >= _BULK_FLOOR_MS


def test_bulk_payload_short_of_threshold_stays_instant(clock):
    session = _make_session()
    payload = "x" * (_BULK_SUBMIT_THRESHOLD_CHARS - 1)

    session.submit_input(payload, True)

    assert clock.sleep_calls == []


def test_bulk_wait_aborts_early_if_session_exits_mid_wait(clock):
    session = _make_session()
    payload = "steer " * 120

    def _on_sleep(c: _FakeClock) -> None:
        session._exited = True

    clock.on_sleep(_on_sleep)

    outcome = session.submit_input(payload, True)

    # The text write already landed (session wasn't exited at that point),
    # but the session died before the submit — must report the drop, not
    # silently claim delivery for a message that never got its submit.
    assert outcome.delivered is False
    assert outcome.reason == INPUT_DROPPED


# ---------------------------------------------------------------- dropped


def test_returns_dropped_when_already_exited(clock):
    session = _make_session()
    session._exited = True

    outcome = session.submit_input("hello", True)

    assert outcome.delivered is False
    assert outcome.reason == INPUT_DROPPED
    session._pty.write.assert_not_called()


# ------------------------------------------------- ingest verification (#760)


def test_bulk_payload_never_echoed_is_not_delivered_and_gets_no_cr(clock):
    """#760's silent drop, pinned.

    The PTY write returns without raising but the terminal never paints the
    payload back. Before #760 that was reported as delivery (and a CR went
    out blind); now it is a NOT-delivered verdict with no CR at all — firing
    one into a terminal that never showed the text can answer whatever modal
    dialog is open instead of submitting anything.
    """
    session = _make_session()
    payload = "CHIEF - finish the issue and commit. " * 20
    # Nothing ever comes back out of the PTY.

    outcome = session.submit_input(payload, True)

    assert outcome.delivered is False
    assert outcome.reason == INPUT_NOT_INGESTED
    assert outcome.ingested is False
    assert outcome.submitted is False
    calls = [c.args[0] for c in session._pty.write.call_args_list]
    assert "".join(calls) == payload  # the text only — no submitting CR
    assert "\r" not in calls
    # It waited for the echo past the settle cap, but stayed bounded.
    total_waited_ms = sum(clock.sleep_calls) * 1000
    assert total_waited_ms >= _BULK_CAP_MS
    assert total_waited_ms < _INGEST_CAP_MS + 200


def test_busy_agent_defers_the_submit_instead_of_firing_a_blind_cr(clock, parked_watchers):
    """The 2026-08-14 stall shape, and #763's fix.

    The agent is working, so its spinner repaints continuously and the stream
    never goes quiet. Before #763 the CR fired at the settle cap anyway, into
    a mid-repaint composer that absorbs it as a literal newline. Now no CR is
    written at all: the submit is handed to a watcher, and the call says so.
    """
    session = _make_session()
    payload = "CHIEF - status check, please report. " * 20

    def _on_sleep(c: _FakeClock) -> None:
        # Echo lands early (the composer shows the paste), then output keeps
        # arriving forever — a working agent's spinner.
        if not session._ring:
            _echo(session, payload)
        session._last_output_at = c.now

    clock.on_sleep(_on_sleep)

    outcome = session.submit_input(payload, True)

    assert outcome.reason == INPUT_DEFERRED
    assert outcome.ingested is True
    assert outcome.deferred is True
    assert outcome.submitted is False
    # Not established, not False: the watcher has not run yet.
    assert outcome.submit_confirmed is None
    # The payload reached the composer, not the agent: until the watcher's CR
    # lands this is not a delivery (#929), and the in-flight state is named.
    assert outcome.delivered is False
    assert outcome.submit_state == "pending"
    # The decisive assertion: no CR was written into the busy terminal.
    writes = [c.args[0] for c in session._pty.write.call_args_list]
    assert "\r" not in writes

    # The agent finishes: the watcher presses Enter once, never resending text.
    clock.on_sleep(lambda c: None)
    assert len(parked_watchers) == 1
    parked_watchers[0].run()

    writes = [c.args[0] for c in session._pty.write.call_args_list]
    assert writes.count("\r") == 1 and writes[-1] == "\r"
    assert "".join(writes[:-1]) == payload
    assert session.to_api()["last_input"]["reason"] == INPUT_OK


# ------------------------------------------------ deferred submit (#763)


def test_deferred_watcher_presses_enter_once_the_agent_settles(clock):
    """#763's whole point: the steer lands without a human at the keyboard.

    The agent stops working, the payload is still sitting in the composer, so
    the watcher writes exactly one bare CR — the same action a human performs
    on a stranded ``[Pasted text #N]`` chip.
    """
    payload = "CHIEF - please finish the scope and commit. " * 12
    session = _make_session()
    needles = session_host_input._echo_needles(payload)
    mark = session._output_total
    # The composer keeps repainting the payload while the agent works, then
    # goes quiet with the chip still showing.
    _echo(session, payload)
    session._last_output_at = clock.now

    outcome = session._await_deferred_submit(session._defer_seq, mark, needles)

    assert outcome is not None
    assert outcome.reason == INPUT_OK
    assert outcome.submitted is True
    assert outcome.submit_confirmed is True
    assert outcome.deferred is True
    session._pty.write.assert_called_once_with("\r")
    # It waited for a genuine quiet window, not just the paste-settle one.
    assert outcome.waited_ms >= _DEFER_QUIET_MS


def test_deferred_watcher_never_resends_the_text(clock):
    """A blind resend of something like "/issue-finish" could double-execute
    (#760's carried-over constraint) — only a bare CR is ever in scope."""
    payload = "CHIEF - /issue-finish now that the gate is green. " * 12
    session = _make_session()
    needles = session_host_input._echo_needles(payload)
    mark = session._output_total
    _echo(session, payload)
    session._last_output_at = clock.now

    session._await_deferred_submit(session._defer_seq, mark, needles)

    written = [c.args[0] for c in session._pty.write.call_args_list]
    assert written == ["\r"]


def test_deferred_watcher_writes_nothing_when_the_payload_is_gone(clock):
    """Quiet came, but the composer no longer shows the payload — it either
    already went or the terminal moved on. Firing a CR at a state we cannot
    identify is exactly what #763 forbids."""
    payload = "CHIEF - a steer that got submitted by hand meanwhile. " * 12
    session = _make_session()
    needles = session_host_input._echo_needles(payload)
    # Only unrelated output since the mark — no chip, no echo.
    mark = session._output_total
    _echo(session, "\x1b[2J\x1b[1;1Hthinking about something else entirely\r\n")
    session._last_output_at = clock.now

    outcome = session._await_deferred_submit(session._defer_seq, mark, needles)

    assert outcome is not None
    assert outcome.reason == INPUT_DEFER_VANISHED
    assert outcome.submitted is False
    assert outcome.submit_confirmed is False
    session._pty.write.assert_not_called()


def test_deferred_watcher_refuses_to_answer_a_dialog(clock):
    """The dangerous case named in #763: a permission / AskUserQuestion modal
    is up, where a bare CR picks a menu option instead of submitting."""
    payload = "CHIEF - go ahead and land the branch. " * 12
    session = _make_session()
    needles = session_host_input._echo_needles(payload)
    mark = session._output_total
    _echo(session, payload)
    _echo(session, "\r\n Do you want to proceed?\r\n ❯ 1. Yes\r\n   2. No\r\n")
    session._last_output_at = clock.now

    outcome = session._await_deferred_submit(session._defer_seq, mark, needles)

    assert outcome is not None
    assert outcome.reason == INPUT_DEFER_UNCLEAR
    assert outcome.submit_confirmed is False
    session._pty.write.assert_not_called()


def test_deferred_watcher_is_bounded_and_gives_up_without_firing(clock):
    """An agent that just keeps working: the window closes, nothing is
    written, and the steer stays stranded but honestly reported — the
    pre-#763 state, never worse."""
    payload = "CHIEF - status? " * 40
    session = _make_session()
    needles = session_host_input._echo_needles(payload)
    mark = session._output_total
    _echo(session, payload)
    clock.on_sleep(lambda c: setattr(session, "_last_output_at", c.now))

    outcome = session._await_deferred_submit(session._defer_seq, mark, needles)

    assert outcome is not None
    assert outcome.reason == INPUT_DEFER_TIMEOUT
    assert outcome.submit_confirmed is False
    # #929: a paste stranded in the composer is not delivered.
    assert outcome.submitted is False
    assert outcome.delivered is False
    assert outcome.submit_state == "not_submitted"
    session._pty.write.assert_not_called()
    assert outcome.waited_ms >= _DEFER_CAP_MS


# ------------------------------------- a window that outlasts a gate (#1075)

# CLAUDE.md's re-baselined full-tier runtime for
# ``scripts/verify-before-ship.ps1`` on this box. A lane running it sits inside
# a single tool call for that whole time, repainting its spinner throughout.
_GATE_RUNTIME_MS = 19 * 60 * 1000


def test_the_deferred_window_outlasts_a_full_tier_gate_run():
    """The budget has to be sized against the thing being waited on (#1075).

    The old 120 s could not reach the end of a turn that spends ~19 min inside
    one tool call, so a steer to a lane mid-gate stranded essentially every
    time. This pins the *intent* against a future casual edit of the constant:
    whatever the number is, it must outlast a gate run.
    """
    assert _DEFER_CAP_MS > _GATE_RUNTIME_MS


def test_a_steer_lands_on_a_target_that_stays_busy_for_a_whole_gate_run(clock):
    """#1075's headline case, and the one that stranded three briefs on
    2026-09-19 (sids 1967cb0f / ca58061c / 76fc29b8).

    The target is running the gate: it repaints continuously for ~19 minutes
    inside one tool call, then finishes and goes quiet with the steer still
    sitting unsent in its composer. The watcher has to still be there to press
    Enter — under the old 120 s cap it had given up seventeen minutes earlier
    and reported ``defer_timeout``.
    """
    payload = "CHIEF - when the gate is green, open the PR as a draft. " * 10
    session = _make_session()
    needles = session_host_input._echo_needles(payload)
    mark = session._output_total
    _echo(session, payload)
    session._last_output_at = clock.now

    busy_until = clock.now + _GATE_RUNTIME_MS / 1000
    repainted = {"done": False}

    def _on_sleep(c: _FakeClock) -> None:
        if c.now < busy_until:
            # A working agent's spinner: output never stops, so the stream
            # never goes quiet and the watcher can only wait.
            _echo(session, ".")
            session._last_output_at = c.now
        elif not repainted["done"]:
            # The tool call returns. The composer repaints one last time with
            # the steer still in it, and then the terminal falls silent.
            repainted["done"] = True
            _echo(session, payload)
            session._last_output_at = c.now

    clock.on_sleep(_on_sleep)

    outcome = session._await_deferred_submit(session._defer_seq, mark, needles)

    assert outcome is not None
    assert outcome.reason == INPUT_OK
    assert outcome.submitted is True
    assert outcome.submit_confirmed is True
    assert outcome.delivered is True
    assert outcome.submit_state == "confirmed"
    # Exactly one bare CR — never a resend of the text (#760's constraint).
    session._pty.write.assert_called_once_with("\r")
    assert outcome.waited_ms >= _GATE_RUNTIME_MS


def test_watcher_declines_a_paste_chip_that_is_not_ours(clock):
    """The one hazard that genuinely scales with a longer window (#1075).

    Raw keystrokes deliberately do not supersede the watcher
    (``PtySession._defer_seq``'s note), so while it waits, the human on the
    phone can paste into the same composer. Before #1075 the pre-fire check
    matched on the bare ``[Pasted text #`` marker, so *their* chip read as
    proof *our* payload was still pending — and the watcher would press Enter
    on their half-composed message. Ours has scrolled out of the lookback
    window by then; theirs is all that is showing.
    """
    payload = "CHIEF - land the branch and report back on the gate. " * 10
    session = _make_session()
    needles = session_host_input._echo_needles(payload)
    mark = session._output_total
    _echo(session, payload)
    session._last_output_at = clock.now

    polls = {"n": 0}

    def _on_sleep(c: _FakeClock) -> None:
        polls["n"] += 1
        if polls["n"] <= 40:
            # Keep it busy long enough that our own echo ages out of the
            # rolling ~_DEFER_FRAME_LOOKBACK_MS window.
            _echo(session, ".")
            session._last_output_at = c.now
        elif polls["n"] == 41:
            # The human's own paste is what the composer now shows.
            _echo(session, "\x1b[2m> [Pasted text #9 +2 lines]\x1b[0m")
            session._last_output_at = c.now

    clock.on_sleep(_on_sleep)

    outcome = session._await_deferred_submit(session._defer_seq, mark, needles)

    assert outcome is not None
    assert outcome.reason == INPUT_DEFER_VANISHED
    assert outcome.submitted is False
    assert outcome.submit_confirmed is False
    assert outcome.delivered is False
    # The decisive assertion: no CR was pressed on somebody else's paste.
    session._pty.write.assert_not_called()


@pytest.mark.parametrize(
    ("repainted_chip", "enter_pressed"), [(7, True), (9, False)], ids=["ours", "theirs"]
)
def test_our_own_paste_chip_is_pinned_at_ingest_and_still_submits(
    clock, parked_watchers, repainted_chip, enter_pressed
):
    """The other half of #1075: pinning must not cost the chip-only case.

    A bulk paste Claude Code collapses is never echoed verbatim, so its chip
    is the only evidence there is. ``_submit_input_locked`` pins that chip's
    *number* while the window still unambiguously belongs to our own write,
    and the watcher accepts it minutes later because the number matches. Once
    a keystroke has touched the composer, only that number can vouch for it:
    our chip on screen gets the Enter, somebody else's gets none.
    """
    session = _make_session()
    payload = "CHIEF - a long steer the composer collapses into a chip. " * 20

    def _on_sleep(c: _FakeClock) -> None:
        if not session._ring:
            _echo(session, "\x1b[2m> [Pasted text #7 +30 lines]\x1b[0m")
        session._last_output_at = c.now

    clock.on_sleep(_on_sleep)

    outcome = session.submit_input(payload, True)

    assert outcome.reason == INPUT_DEFERRED
    session.write("\x1b[A")  # the human recalls history: the composer is touched
    session._pty.write.reset_mock()

    polls = {"n": 0}

    def _agent_finishes(c: _FakeClock) -> None:
        polls["n"] += 1
        if polls["n"] <= 40:
            # Busy long enough for the original echo to age out of the window.
            _echo(session, ".")
            session._last_output_at = c.now
        elif polls["n"] == 41:
            _echo(session, f"\x1b[2m> [Pasted text #{repainted_chip} +30 lines]\x1b[0m")
            session._last_output_at = c.now

    clock.on_sleep(_agent_finishes)
    parked_watchers[0].run()

    writes = [c.args[0] for c in session._pty.write.call_args_list]
    assert writes == (["\r"] if enter_pressed else [])
    assert session.to_api()["last_input"]["reason"] == (
        INPUT_OK if enter_pressed else INPUT_DEFER_VANISHED
    )


def test_a_pinned_chip_number_is_not_matched_by_a_longer_one(clock):
    """``#1`` must not match ``#12`` — without a trailing boundary the pin
    degrades back into the any-chip-will-do check it replaces."""
    normalized = session_host_input._normalize_echo(
        "\x1b[2m> [Pasted text #12 +4 lines]\x1b[0m"
    )

    assert session_host_input.chip_visible(normalized, "[pastedtext#12") is True
    assert session_host_input.chip_visible(normalized, "[pastedtext#1") is False
