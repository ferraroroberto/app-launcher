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
    _INGEST_CAP_MS,
    INPUT_DROPPED,
    INPUT_NOOP,
    INPUT_NOT_INGESTED,
    INPUT_OK,
    INPUT_SETTLE_CAP,
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


def test_busy_agent_that_never_goes_quiet_reports_submit_unconfirmed(clock):
    """The 2026-08-14 stall shape: the agent is working, so its spinner
    repaints continuously and the stream never goes quiet. The CR still
    fires at the settle cap (unchanged behaviour), but the outcome says the
    submit could not be confirmed instead of reporting plain success."""
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

    assert outcome.reason == INPUT_SETTLE_CAP
    assert outcome.ingested is True
    assert outcome.submitted is True
    assert outcome.submit_confirmed is False
    # delivered stays true — the payload really did reach the terminal; it is
    # the *submit* that is unconfirmed, and the two are reported separately.
    assert outcome.delivered is True
    assert [c.args[0] for c in session._pty.write.call_args_list][-1] == "\r"


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
