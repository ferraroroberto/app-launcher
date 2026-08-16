"""``PtySession.submit_input`` + DECSET 2004 tracking — issue #611.

Ports the compose bar's ``framePaste``/``sendSubmit``/``bulkSettle``
(``app/webapp/static/terminal-compose.js``, issues #166/#450/#499) to the
HTTP ``/input`` path, which previously wrote text and a CR back-to-back with
no settle logic at all — Claude Code's composer classifies a bulk write as a
paste, and a CR landing mid-ingest is absorbed as a literal newline instead
of Submit, stranding the message unsent.

These tests pin the ported behaviour against a fake PTY + a controllable
fake clock, so a future refactor can't quietly reintroduce the swallow.
"""

from __future__ import annotations

import time as real_time
from unittest.mock import MagicMock

import pytest

from src.session_host import (
    _BULK_CAP_MS,
    _BULK_FLOOR_MS,
    _BULK_QUIET_MS,
    _BULK_SUBMIT_THRESHOLD_CHARS,
    _DEFER_CAP_MS,
    _DEFER_QUIET_MS,
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
    _scan_bracketed_paste_mode,
    PtySession,
)
from src import session_host as session_host_module


def _make_session() -> PtySession:
    pty = MagicMock(name="PtyProcess")
    return PtySession(
        session_id="sid-test",
        project_dir=r"C:\stub",
        name="claude",
        flags="",
        started_at=real_time.time(),
        _loop=MagicMock(),
        _pty=pty,
    )


def _echo(session: PtySession, text: str) -> None:
    """Simulate the reader thread painting ``text`` back out of the PTY.

    The real ingest signal (#760) is what the terminal *paints*, so a test
    that wants a bulk payload to submit has to echo it — same as a live
    Claude Code composer echoing a paste or collapsing it into a chip.
    """
    with session._ring_lock:
        session._output_total += len(text)
        session._ring += text


class _FakeClock:
    """A controllable time.time()/time.sleep() double, module-patched onto
    src.session_host so submit_input's settle wait is deterministic and
    instant instead of racing a real wall clock."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start
        self.sleep_calls: list[float] = []
        self._on_sleep = None  # optional callback(clock) fired each sleep()

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.now += seconds
        if self._on_sleep is not None:
            self._on_sleep(self)

    def on_sleep(self, fn) -> None:
        self._on_sleep = fn


@pytest.fixture
def clock(monkeypatch):
    fake = _FakeClock()
    monkeypatch.setattr(session_host_module.time, "time", fake.time)
    monkeypatch.setattr(session_host_module.time, "sleep", fake.sleep)
    return fake


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


def test_busy_agent_defers_the_submit_instead_of_firing_a_blind_cr(clock, monkeypatch):
    """The 2026-08-14 stall shape, and #763's fix.

    The agent is working, so its spinner repaints continuously and the stream
    never goes quiet. Before #763 the CR fired at the settle cap anyway, into
    a mid-repaint composer that absorbs it as a literal newline. Now no CR is
    written at all: the submit is handed to a watcher, and the call says so.
    """
    session = _make_session()
    payload = "CHIEF - status check, please report. " * 20
    armed: list = []
    monkeypatch.setattr(
        PtySession, "_arm_deferred_submit", lambda self, *a, **kw: armed.append((a, kw))
    )

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
    # delivered stays true — the payload really did reach the terminal; it is
    # the *submit* that is still outstanding, and the two are reported apart.
    assert outcome.delivered is True
    # The decisive assertion: no CR was written into the busy terminal.
    assert "\r" not in [c.args[0] for c in session._pty.write.call_args_list]
    assert len(armed) == 1


# ------------------------------------------------ deferred submit (#763)


def test_deferred_watcher_presses_enter_once_the_agent_settles(clock):
    """#763's whole point: the steer lands without a human at the keyboard.

    The agent stops working, the payload is still sitting in the composer, so
    the watcher writes exactly one bare CR — the same action a human performs
    on a stranded ``[Pasted text #N]`` chip.
    """
    payload = "CHIEF - please finish the scope and commit. " * 12
    session = _make_session()
    needles = session_host_module._echo_needles(payload)
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
    needles = session_host_module._echo_needles(payload)
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
    needles = session_host_module._echo_needles(payload)
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
    needles = session_host_module._echo_needles(payload)
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
    needles = session_host_module._echo_needles(payload)
    mark = session._output_total
    _echo(session, payload)
    clock.on_sleep(lambda c: setattr(session, "_last_output_at", c.now))

    outcome = session._await_deferred_submit(session._defer_seq, mark, needles)

    assert outcome is not None
    assert outcome.reason == INPUT_DEFER_TIMEOUT
    assert outcome.submit_confirmed is False
    session._pty.write.assert_not_called()
    assert outcome.waited_ms >= _DEFER_CAP_MS


def test_a_newer_write_supersedes_a_pending_watcher(clock):
    """Somebody else wrote to the PTY, so the watcher's "my payload is the
    thing sitting unsent" premise no longer holds. It exits without firing
    and without touching ``last_input``, which now describes the newer call."""
    payload = "CHIEF - the first steer. " * 20
    session = _make_session()
    needles = session_host_module._echo_needles(payload)
    mark = session._output_total
    _echo(session, payload)
    session._last_output_at = clock.now
    stale_seq = session._defer_seq
    session._defer_seq += 1  # what a newer submit_input() does

    outcome = session._await_deferred_submit(stale_seq, mark, needles)

    assert outcome is None
    session._pty.write.assert_not_called()


def test_stop_cancels_a_pending_watcher(clock):
    """A CR landing in the middle of an interrupt or a "/quit" sequence would
    be answering a terminal state the watcher never verified."""
    session = _make_session()
    seq_before = session._defer_seq

    session.stop(mode="interrupt")

    assert session._defer_seq != seq_before


def test_deferred_verdict_is_recorded_on_the_session(clock):
    """The watcher's outcome has to land on ``last_input`` in the same shape
    as an immediate write, or a caller polling for the final verdict sees the
    stale ``deferred`` one forever."""
    payload = "CHIEF - report when done. " * 20
    session = _make_session()
    needles = session_host_module._echo_needles(payload)
    mark = session._output_total
    _echo(session, payload)
    session._last_output_at = clock.now

    session._run_deferred_submit(session._defer_seq, mark, needles, len(payload))

    last_input = session.to_api()["last_input"]
    assert last_input["reason"] == INPUT_OK
    assert last_input["deferred"] is True
    assert last_input["submit_confirmed"] is True
    assert last_input["bytes"] == len(payload)
    assert last_input["submit"] is True


def test_paste_chip_counts_as_ingest_evidence(clock):
    """Claude Code collapses a bulk paste into "[Pasted text #N +M lines]"
    instead of echoing it, so the chip is the only evidence there is."""
    session = _make_session()
    payload = "CHIEF - a long steer that gets collapsed. " * 20

    def _on_sleep(c: _FakeClock) -> None:
        if not session._ring:
            _echo(session, "\x1b[2m> [Pasted text #2 +53 lines]\x1b[0m\r\n")
            session._last_output_at = c.now

    clock.on_sleep(_on_sleep)

    outcome = session.submit_input(payload, True)

    assert outcome.ingested is True
    assert outcome.submitted is True


def test_echo_is_matched_through_wrapping_and_escape_decoration(clock):
    """A composer echo comes back hard-wrapped inside a box-drawn frame with
    SGR runs through it — the match has to survive all of that."""
    session = _make_session()
    payload = "CHIEF - please finish the scope and commit your work now. " * 10

    def _on_sleep(c: _FakeClock) -> None:
        if not session._ring:
            decorated = "\x1b[38;5;250m│\x1b[0m ".join(
                payload[i : i + 40] + "\r\n  " for i in range(0, 200, 40)
            )
            _echo(session, "\x1b[1;1H" + decorated)
            session._last_output_at = c.now

    clock.on_sleep(_on_sleep)

    outcome = session.submit_input(payload, True)

    assert outcome.ingested is True


def test_identical_earlier_output_is_not_mistaken_for_this_echo(clock):
    """The chief resends a near-identical brief when the first is stranded.
    Evidence has to come from output painted *after* this write, or the
    resend would verify itself against the first attempt's echo."""
    session = _make_session()
    payload = "CHIEF - Setup message, establishing a convention. " * 12
    _echo(session, payload)  # the earlier attempt's echo, already in the ring

    outcome = session.submit_input(payload, True)

    assert outcome.reason == INPUT_NOT_INGESTED
    assert outcome.ingested is False


def test_outcome_is_recorded_on_the_session_and_exposed_to_the_api(clock):
    """A session that has gone deaf to API input must be detectable as such
    without a human trying the keyboard (#760's second acceptance point)."""
    session = _make_session()
    payload = "CHIEF - do the thing. " * 30

    assert session.to_api()["last_input"] is None

    session.submit_input(payload, True)

    last_input = session.to_api()["last_input"]
    assert last_input["reason"] == INPUT_NOT_INGESTED
    assert last_input["delivered"] is False
    assert last_input["bytes"] == len(payload)
    assert last_input["submit"] is True
    assert last_input["at"] > 0


# ------------------------------------------------------- DECSET 2004 scan


def test_scan_detects_enable():
    latest, carry = _scan_bracketed_paste_mode("\x1b[?2004h", "")
    assert latest is True
    assert carry == ""


def test_scan_detects_disable():
    latest, carry = _scan_bracketed_paste_mode("\x1b[?2004l", "")
    assert latest is False
    assert carry == ""


def test_scan_ignores_unrelated_escape_sequences():
    latest, carry = _scan_bracketed_paste_mode("\x1b[2J\x1b[1;1H", "")
    assert latest is None
    assert carry == ""


def test_scan_returns_latest_when_multiple_in_one_chunk():
    latest, _ = _scan_bracketed_paste_mode("\x1b[?2004h...\x1b[?2004l", "")
    assert latest is False


def test_scan_handles_sequence_split_across_reads():
    latest1, carry = _scan_bracketed_paste_mode("hello\x1b[?20", "")
    assert latest1 is None
    assert carry == "\x1b[?20"
    latest2, carry2 = _scan_bracketed_paste_mode("04h world", carry)
    assert latest2 is True
    assert carry2 == ""


def test_scan_fast_path_no_escape_no_carry():
    latest, carry = _scan_bracketed_paste_mode("plain output, no escapes", "")
    assert latest is None
    assert carry == ""
