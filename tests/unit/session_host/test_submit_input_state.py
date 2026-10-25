"""PtySession.submit_input: image paths, the recorded verdict, echo matching and DECSET 2004 tracking."""

from __future__ import annotations


import pytest

from src.session_host import _scan_bracketed_paste_mode, PtySession
from src.session_host_input import (
    _BULK_CAP_MS,
    INPUT_DEFER_TIMEOUT,
    INPUT_DEFER_UNCLEAR,
    INPUT_DEFER_VANISHED,
    INPUT_DEFERRED,
    INPUT_DROPPED,
    INPUT_NOOP,
    INPUT_NOT_INGESTED,
    INPUT_OK,
    INPUT_UNVERIFIED,
    InputOutcome,
)
from src import session_host_input

from tests.unit.session_host._support import _FakeClock, _echo, _make_session


# ------------------------------------------------- image attachments (#1212)

_IMAGE_PATH = r"C:\stub\.launcher-tmp\20260924-120000-abc123-shot.png"


def _record_writes(session: PtySession, clock: _FakeClock) -> list:
    """Capture every PTY write with the fake-clock time it landed at."""
    writes: list = []
    session._pty.write.side_effect = lambda data: writes.append((data, clock.now))
    return writes


def test_image_path_submits_only_after_the_chip_is_painted(clock):
    """#1212's repro shape: a short prompt plus an attached image path.

    Claude Code paints "Pasting…" at once, goes quiet while it converts the
    path, and replaces it with the "[Image #N]" chip only when done. A CR in
    that gap is absorbed and the prompt sits unsent — which is what the
    instant short-send path did. The chip is also the only ingest evidence:
    the path is never echoed and the text is too short to make a needle.
    """
    session = _make_session()
    session._bracketed_paste_mode = True
    payload = "reply with just the word ok\n\n" + _IMAGE_PATH
    writes = _record_writes(session, clock)
    sent_at = clock.now
    chip_at: dict = {}

    def _on_sleep(c: _FakeClock) -> None:
        elapsed_ms = (c.now - sent_at) * 1000
        if not session._ring:
            _echo(session, "\x1b[2mPasting…\x1b[0m")
            session._last_output_at = c.now
        elif "t" not in chip_at and elapsed_ms >= 900:
            _echo(session, "❯ [Image\x1b[1C#4]reply with just the word ok")
            session._last_output_at = c.now
            chip_at["t"] = c.now

    clock.on_sleep(_on_sleep)

    outcome = session.submit_input(payload, True)

    assert outcome.reason == INPUT_OK
    assert outcome.ingested is True
    assert outcome.to_api()["submit_state"] == "confirmed"
    assert outcome.delivered is True
    assert writes[-1][0] == "\r"
    assert writes[-1][1] > chip_at["t"]


def test_image_conversion_in_flight_holds_the_cr_past_a_quiet_window(clock):
    """The text half echoes at once, so the payload is ingested before the
    chip — and the stream then sits quiet well past _BULK_QUIET_MS while the
    image converts. Quiet alone must not count as settled while a "Pasting…"
    has no chip after it (#1211's rule, ported)."""
    session = _make_session()
    session._bracketed_paste_mode = True
    payload = "please describe what this screenshot shows\n\n" + _IMAGE_PATH
    writes = _record_writes(session, clock)
    sent_at = clock.now
    chip_at: dict = {}

    def _on_sleep(c: _FakeClock) -> None:
        elapsed_ms = (c.now - sent_at) * 1000
        if not session._ring:
            _echo(session, "❯ please describe what this screenshot shows Pasting…")
            session._last_output_at = c.now
        elif "t" not in chip_at and elapsed_ms >= 2000:
            _echo(session, "❯ [Image #2]please describe what this screenshot shows")
            session._last_output_at = c.now
            chip_at["t"] = c.now

    clock.on_sleep(_on_sleep)

    outcome = session.submit_input(payload, True)

    assert outcome.reason == INPUT_OK
    assert outcome.submit_confirmed is True
    assert writes[-1][0] == "\r"
    assert writes[-1][1] > chip_at["t"]


def test_image_path_echoed_as_text_settles_on_the_plain_rule(clock):
    """An agent that does not convert paths echoes the path verbatim: no
    "Pasting…", no chip, and the echo-then-quiet rule confirms it."""
    session = _make_session()
    payload = "look at this\n\n" + _IMAGE_PATH
    writes = _record_writes(session, clock)

    def _on_sleep(c: _FakeClock) -> None:
        if not session._ring:
            _echo(session, "> look at this\r\n  " + _IMAGE_PATH)
            session._last_output_at = c.now

    clock.on_sleep(_on_sleep)

    outcome = session.submit_input(payload, True)

    assert outcome.reason == INPUT_OK
    assert outcome.submit_confirmed is True
    assert writes[-1][0] == "\r"
    assert sum(clock.sleep_calls) * 1000 < _BULK_CAP_MS


def test_busy_agent_pins_the_image_chip_for_the_watcher(clock, monkeypatch):
    """A busy agent still gets the deferred watcher, and the watcher knows
    the "[Image #N]" chip as this payload — not any image chip."""
    session = _make_session()
    payload = "ok?\n\n" + _IMAGE_PATH
    armed: list = []
    monkeypatch.setattr(
        PtySession, "_arm_deferred_submit", lambda self, *a, **kw: armed.append(a)
    )

    def _on_sleep(c: _FakeClock) -> None:
        if not session._ring:
            _echo(session, "Pasting…❯ [Image #4]ok?")
        session._last_output_at = c.now  # spinner never stops

    clock.on_sleep(_on_sleep)

    outcome = session.submit_input(payload, True)

    assert outcome.reason == INPUT_DEFERRED
    assert outcome.submit_state == "pending"
    seq, mark, needles, chip_id, _touches = armed[0]
    assert chip_id == "[image#4"

    # Another image chip alone is not ours: the watcher must not fire.
    normalized = session_host_input._normalize_echo("❯ [Image #5]")
    assert session._payload_still_visible(normalized, needles, chip_id) is False

    # The agent finishes; our chip is still in the composer, and it goes quiet.
    clock.on_sleep(lambda c: None)
    session._last_output_at = clock.now
    session._pty.write.reset_mock()

    settled = session._await_deferred_submit(seq, mark, needles, chip_id)

    assert settled is not None
    assert settled.reason == INPUT_OK
    assert settled.submitted is True
    session._pty.write.assert_called_once_with("\r")


@pytest.mark.parametrize(
    "data, expected",
    [
        ("hello\n\n" + _IMAGE_PATH, True),
        (_IMAGE_PATH.upper().replace(".PNG", ".JPEG"), True),
        ("see /home/stub/.launcher-tmp/pic.webp", False),  # not its own line
        ("hello\n\n/home/stub/.launcher-tmp/pic.webp", True),
        ("hello\n\n" + r"C:\stub\.launcher-tmp\notes.pdf", False),
        ("rename shot.png to final.png", False),
        ("hello", False),
    ],
)
def test_image_path_detection(data, expected):
    """Only a line that is an absolute image path routes to the settle path;
    short plain-text sends stay instant."""
    assert session_host_input._carries_image_path(data) is expected


@pytest.mark.parametrize(
    "outcome, submit_state, delivered",
    [
        # Submitted and confirmed.
        (dict(reason=INPUT_OK, ingested=True, submitted=True, submit_confirmed=True),
         "confirmed", True),
        # Submitted, confirmation not established.
        (dict(reason=INPUT_UNVERIFIED, submitted=True), "unconfirmed", True),
        # In flight with the watcher.
        (dict(reason=INPUT_DEFERRED, ingested=True, deferred=True), "pending", False),
        # Never submitted — every watcher give-up and both immediate negatives.
        (dict(reason=INPUT_DEFER_TIMEOUT, ingested=True, deferred=True,
              submit_confirmed=False), "not_submitted", False),
        (dict(reason=INPUT_DEFER_VANISHED, ingested=True, deferred=True,
              submit_confirmed=False), "not_submitted", False),
        (dict(reason=INPUT_DEFER_UNCLEAR, ingested=True, deferred=True,
              submit_confirmed=False), "not_submitted", False),
        (dict(reason=INPUT_NOT_INGESTED, ingested=False), "not_submitted", False),
        (dict(reason=INPUT_DROPPED), "not_submitted", False),
        # No submit asked for: a paste that landed is the whole job.
        (dict(reason=INPUT_OK, ingested=True, submit_requested=False),
         "not_requested", True),
        (dict(reason=INPUT_UNVERIFIED, submit_requested=False), "not_requested", True),
        (dict(reason=INPUT_NOOP, submit_requested=False), "not_requested", True),
        (dict(reason=INPUT_DROPPED, submit_requested=False), "not_requested", False),
    ],
)
def test_submit_state_and_delivered_never_disagree(outcome, submit_state, delivered):
    """#929's contract: ``delivered`` is never more optimistic than the fields
    it summarises — no outcome carries ``delivered: true`` next to a submit
    that was asked for and never happened — and ``submit_state`` alone tells
    confirmed / unconfirmed / pending / never-submitted apart."""
    result = InputOutcome(**outcome)

    assert result.submit_state == submit_state
    assert result.delivered is delivered
    api = result.to_api()
    assert api["submit_state"] == submit_state
    assert api["delivered"] is delivered
    assert not (api["delivered"] and outcome.get("submit_requested", True)
                and not api["submitted"])


def test_submit_input_records_whether_a_submit_was_asked_for(clock):
    """``submit_requested`` is stamped from the call itself, so a plain paste
    reads ``not_requested`` rather than ``not_submitted``."""
    session = _make_session()

    draft = session.submit_input("draft", False)
    sent = session.submit_input("hello", True)

    assert draft.submit_state == "not_requested"
    assert draft.delivered is True
    assert sent.submit_state == "unconfirmed"
    assert sent.delivered is True


def test_a_newer_write_supersedes_a_pending_watcher(clock):
    """Somebody else wrote to the PTY, so the watcher's "my payload is the
    thing sitting unsent" premise no longer holds. It exits without firing
    and without touching ``last_input``, which now describes the newer call."""
    payload = "CHIEF - the first steer. " * 20
    session = _make_session()
    needles = session_host_input._echo_needles(payload)
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
    needles = session_host_input._echo_needles(payload)
    mark = session._output_total
    _echo(session, payload)
    session._last_output_at = clock.now

    session._run_deferred_submit(
        session._defer_seq, mark, needles, None, nbytes=len(payload)
    )

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
