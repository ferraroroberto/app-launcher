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

    delivered = session.submit_input("hello", True)

    assert delivered is True
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

    delivered = session.submit_input("", True)

    assert delivered is True
    session._pty.write.assert_called_once_with("\r")


def test_blank_data_without_submit_is_a_true_noop(clock):
    session = _make_session()

    delivered = session.submit_input("", False)

    assert delivered is True
    session._pty.write.assert_not_called()


# ------------------------------------------------------------------- bulk


def test_bulk_payload_waits_for_echo_then_quiet_before_submitting(clock):
    """#499's echo-then-quiet protocol: the CR is held until output arrives
    after the send AND has been silent for _BULK_QUIET_MS, not just a fixed
    delay."""
    session = _make_session()
    payload = "x" * _BULK_SUBMIT_THRESHOLD_CHARS

    # Simulate the reader thread: echo arrives once the floor has passed,
    # then goes quiet. Scripted via the fake clock's sleep callback.
    state = {"echoed": False}
    sent_at_holder = {"t": clock.now}

    def _on_sleep(c: _FakeClock) -> None:
        elapsed_ms = (c.now - sent_at_holder["t"]) * 1000
        if not state["echoed"] and elapsed_ms >= _BULK_FLOOR_MS:
            session._last_output_at = c.now
            state["echoed"] = True

    clock.on_sleep(_on_sleep)

    delivered = session.submit_input(payload, True)

    assert delivered is True
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


def test_bulk_payload_caps_out_if_output_never_settles(clock):
    """If the session's output never goes quiet (or never arrives at all),
    the CR still fires at the cap rather than hanging forever."""
    session = _make_session()
    payload = "x" * _BULK_SUBMIT_THRESHOLD_CHARS
    # No _last_output_at update at all — output never arrives.

    delivered = session.submit_input(payload, True)

    assert delivered is True
    total_waited_ms = sum(clock.sleep_calls) * 1000
    assert total_waited_ms >= _BULK_CAP_MS
    # Bounded — the poll loop must not run indefinitely past the cap.
    assert total_waited_ms < _BULK_CAP_MS + 200
    calls = [c.args[0] for c in session._pty.write.call_args_list]
    assert calls[-1] == "\r"


def test_bulk_payload_short_of_threshold_stays_instant(clock):
    session = _make_session()
    payload = "x" * (_BULK_SUBMIT_THRESHOLD_CHARS - 1)

    session.submit_input(payload, True)

    assert clock.sleep_calls == []


def test_bulk_wait_aborts_early_if_session_exits_mid_wait(clock):
    session = _make_session()
    payload = "x" * _BULK_SUBMIT_THRESHOLD_CHARS

    def _on_sleep(c: _FakeClock) -> None:
        session._exited = True

    clock.on_sleep(_on_sleep)

    delivered = session.submit_input(payload, True)

    # The text write already landed (session wasn't exited at that point),
    # but the final CR write() sees _exited and drops — must report False,
    # not silently claim delivery for a message that never got its submit.
    assert delivered is False


# ---------------------------------------------------------------- dropped


def test_returns_false_when_already_exited(clock):
    session = _make_session()
    session._exited = True

    delivered = session.submit_input("hello", True)

    assert delivered is False
    session._pty.write.assert_not_called()


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
