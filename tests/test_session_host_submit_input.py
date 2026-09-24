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

from src.session_host import _scan_bracketed_paste_mode, PtySession
from src.session_host_input import (
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
    InputOutcome,
)
from src import session_host as session_host_module
from src import session_host_input


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
    # The payload reached the composer, not the agent: until the watcher's CR
    # lands this is not a delivery (#929), and the in-flight state is named.
    assert outcome.delivered is False
    assert outcome.submit_state == "pending"
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


def test_our_own_paste_chip_is_pinned_at_ingest_and_still_submits(clock, monkeypatch):
    """The other half of #1075: pinning must not cost the chip-only case.

    A bulk paste Claude Code collapses is never echoed verbatim, so its chip
    is the only evidence there is. ``_submit_input_locked`` pins that chip's
    *number* while the window still unambiguously belongs to our own write,
    and the watcher accepts it minutes later because the number matches.
    """
    session = _make_session()
    payload = "CHIEF - a long steer the composer collapses into a chip. " * 20
    armed: list = []
    monkeypatch.setattr(
        PtySession, "_arm_deferred_submit", lambda self, *a, **kw: armed.append(a)
    )

    def _on_sleep(c: _FakeClock) -> None:
        if not session._ring:
            _echo(session, "\x1b[2m> [Pasted text #7 +30 lines]\x1b[0m")
        session._last_output_at = c.now

    clock.on_sleep(_on_sleep)

    outcome = session.submit_input(payload, True)

    assert outcome.reason == INPUT_DEFERRED
    seq, mark, needles, chip_id = armed[0]
    assert chip_id == "[pastedtext#7"

    # The agent finishes; the composer still shows our chip, and goes quiet.
    clock.on_sleep(lambda c: None)
    session._last_output_at = clock.now
    session._pty.write.reset_mock()

    settled = session._await_deferred_submit(seq, mark, needles, chip_id)

    assert settled is not None
    assert settled.reason == INPUT_OK
    assert settled.submitted is True
    assert settled.delivered is True
    session._pty.write.assert_called_once_with("\r")


def test_a_pinned_chip_number_is_not_matched_by_a_longer_one(clock):
    """``#1`` must not match ``#12`` — without a trailing boundary the pin
    degrades back into the any-chip-will-do check it replaces."""
    normalized = session_host_input._normalize_echo(
        "\x1b[2m> [Pasted text #12 +4 lines]\x1b[0m"
    )

    assert session_host_input._chip_visible(normalized, "[pastedtext#12") is True
    assert session_host_input._chip_visible(normalized, "[pastedtext#1") is False


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
    seq, mark, needles, chip_id = armed[0]
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
        session._defer_seq, mark, needles, None, len(payload)
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
