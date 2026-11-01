"""Messages a busy agent has not taken yet, and discarding them.

The Chat view lists what a session still holds — a payload waiting in the
composer for the deferred watcher, or a message the agent queued mid-turn —
and can discard each. These tests drive the session's real reader thread
with the scripted PTY of ``test_bg_agents_submit``, so the turn state the
list and the discard read comes off the stream the way the session-host
reads it.

The keys a discard presses are the ones a throwaway Claude Code v2.1.316
probe showed remove a queued message without submitting it: Up pulls the
queue back into the composer and Ctrl-U clears it. Never an Enter.
"""

from __future__ import annotations

from src.session_host_input import INPUT_DISCARD_PENDING, INPUT_DISCARDED
from src.session_hosting import pending_discard
from src.session_hosting.pending_input import PendingInputs
from tests.unit.session_host.test_bg_agents_submit import (  # noqa: F401 — fixtures
    _SPINNER_FRAME,
    _TURN_START_FRAME,
    scripted,
)
from tests.unit.session_host.test_typeahead_submit import (
    _CHIP_FRAME,
    _DIALOG_FRAME,
    _QUEUED_FRAME,
    _STEER,
    _defer_steer,
    _working,
)

_UP = "\x1b[A"
_CTRL_U = "\x15"
_SHORT = "also check the ferry times"


def _pending(session) -> list:
    return session.to_api()["pending_input"]


def _queue_short(session, pty) -> dict:
    """A short message sent while the turn runs, then Claude Code showing it
    queued. Returns its pending entry."""
    pty.paint(_TURN_START_FRAME)
    session.submit_input(_SHORT, True)
    pty.paint(_QUEUED_FRAME)
    pty.paint(_SPINNER_FRAME)
    [entry] = _pending(session)
    return entry


def _writes_after(pty, start: int) -> list:
    return pty.writes[start:]


# ------------------------------------------------------------------ listing


def test_a_message_sent_mid_turn_is_listed_queued_once_the_agent_shows_it(scripted):
    session, pty, _clock = scripted
    pty.paint(_TURN_START_FRAME)

    session.submit_input(_SHORT, True)
    [entry] = _pending(session)
    assert entry["state"] == "queued"
    assert entry["text"] == _SHORT
    # Not confirmed queued yet, so a discard would be guessing.
    assert entry["removable"] is False

    pty.paint(_QUEUED_FRAME)
    [entry] = _pending(session)
    assert entry["removable"] is True


def test_a_message_sent_to_an_idle_agent_is_not_pending(scripted):
    session, _pty, _clock = scripted
    session.submit_input(_SHORT, True)
    assert _pending(session) == []


def test_a_queued_message_is_gone_once_the_agent_is_idle(scripted):
    """An agent idle at its prompt has submitted its queue."""
    session, pty, clock = scripted
    _queue_short(session, pty)
    clock.now += 2
    assert _pending(session) == []


def test_a_queued_message_is_kept_while_a_dialog_sits_over_the_queue(scripted):
    """A permission prompt mid-turn is quiet too, but the queue is still
    waiting behind it."""
    session, pty, clock = scripted
    _queue_short(session, pty)
    pty.paint(_DIALOG_FRAME)
    clock.now += 5
    assert [e["state"] for e in _pending(session)] == ["queued"]


def test_a_mid_turn_send_nothing_shows_queued_is_dropped(scripted):
    """No queued marker within the confirm window: the agent took it at
    once, or absorbed it. Either way it is not waiting."""
    session, pty, clock = scripted
    pty.paint(_TURN_START_FRAME)
    session.submit_input(_SHORT, True)
    clock.now += 11
    pty.paint(_SPINNER_FRAME)
    assert _pending(session) == []


def test_a_deferred_payload_is_listed_in_the_composer_then_queued(scripted, monkeypatch):
    session, pty, clock = scripted
    pty.paint(_TURN_START_FRAME)
    args, kw = _defer_steer(session, pty, clock, monkeypatch, _CHIP_FRAME, _working(pty))

    [entry] = _pending(session)
    assert entry["state"] == "in_composer"
    assert entry["removable"] is True

    session._run_deferred_submit(*args, **kw)
    [entry] = _pending(session)
    assert entry["state"] == "queued"
    assert entry["removable"] is True


def test_a_stop_clears_the_list(scripted):
    session, pty, _clock = scripted
    _queue_short(session, pty)
    session.stop(mode="interrupt")
    assert _pending(session) == []


def test_an_agent_whose_queue_was_not_probed_lists_but_cannot_discard(scripted, monkeypatch):
    session, pty, clock = scripted
    session.agent = "codex"
    _defer_steer(session, pty, clock, monkeypatch, _CHIP_FRAME, lambda _n: pty.paint(_SPINNER_FRAME))

    [entry] = _pending(session)
    assert entry["state"] == "in_composer"
    assert entry["removable"] is False
    status, body = session.discard_pending_input(entry["id"])
    assert (status, body["reason"]) == (409, "not_removable")


# ------------------------------------------------------- discarding a queue


def test_discarding_a_queued_message_presses_up_then_ctrl_u_and_never_enter(scripted):
    session, pty, _clock = scripted
    entry = _queue_short(session, pty)
    start = len(pty.writes)

    status, body = session.discard_pending_input(entry["id"])

    assert (status, body["reason"], body["ids"]) == (200, "discarded", [entry["id"]])
    keys = _writes_after(pty, start)
    assert keys[0] == _UP
    assert set(keys[1:]) == {_CTRL_U}
    assert "\r" not in keys
    assert _pending(session) == []
    assert session.to_api()["composer_dirty"] is False


def test_one_discard_takes_the_whole_queue(scripted):
    """Up pulls every queued message out at once, so they all go."""
    session, pty, _clock = scripted
    first = _queue_short(session, pty)
    session.submit_input("and the bus times", True)
    pty.paint(_QUEUED_FRAME)
    pty.paint(_SPINNER_FRAME)
    assert len(_pending(session)) == 2

    status, body = session.discard_pending_input(first["id"])

    assert status == 200
    assert len(body["ids"]) == 2
    assert _pending(session) == []


def test_no_key_goes_in_between_turns(scripted):
    """No live spinner: the turn may be yielding and taking the queue, and
    Up on an idle prompt recalls history. Nothing is pressed."""
    session, pty, clock = scripted
    entry = _queue_short(session, pty)
    clock.now += 1.2
    start = len(pty.writes)

    status, body = session.discard_pending_input(entry["id"])

    assert (status, body["reason"]) == (409, "not_mid_turn")
    assert _writes_after(pty, start) == []


def test_no_key_goes_in_over_a_half_typed_message(scripted):
    session, pty, _clock = scripted
    entry = _queue_short(session, pty)
    session.write("half a thought")
    start = len(pty.writes)

    status, body = session.discard_pending_input(entry["id"])

    assert (status, body["reason"]) == (409, "composer_has_text")
    assert _writes_after(pty, start) == []


def test_no_key_goes_in_over_a_dialog(scripted):
    session, pty, _clock = scripted
    entry = _queue_short(session, pty)
    pty.paint(_DIALOG_FRAME)
    pty.paint(_SPINNER_FRAME)
    start = len(pty.writes)

    status, body = session.discard_pending_input(entry["id"])

    assert (status, body["reason"]) == (409, "dialog_open")
    assert _writes_after(pty, start) == []


def test_a_message_already_taken_is_a_404(scripted):
    session, _pty, _clock = scripted
    status, body = session.discard_pending_input("nosuchentry")
    assert (status, body["reason"]) == (404, "not_pending")


# ---------------------------------------------- discarding from the composer


def test_a_payload_in_the_composer_is_cleared_by_the_watcher_not_submitted(scripted, monkeypatch):
    session, pty, clock = scripted
    pty.paint(_TURN_START_FRAME)
    args, kw = _defer_steer(session, pty, clock, monkeypatch, _CHIP_FRAME, _working(pty))
    [entry] = _pending(session)

    status, body = session.discard_pending_input(entry["id"])
    assert (status, body["reason"]) == (202, "discarding")
    assert _pending(session)[0]["state"] == "discarding"

    start = len(pty.writes)
    session._run_deferred_submit(*args, **kw)

    keys = _writes_after(pty, start)
    assert keys and set(keys) == {_CTRL_U}
    assert len(keys) == 2 * (_STEER.count("\n") + 1) + 6
    assert session.last_input["reason"] == INPUT_DISCARDED
    assert session.last_input["delivered"] is False
    assert _pending(session) == []


def test_a_discard_while_the_watcher_confirms_its_enter_finds_it_already_sent(scripted, monkeypatch):
    """The watcher's Enter is written under the lock, then confirmed outside
    it for up to 10 s. A discard in that window must not be told the
    payload is being cleared from an input box it already left."""
    session, pty, clock = scripted
    pty.paint(_TURN_START_FRAME)
    args, kw = _defer_steer(session, pty, clock, monkeypatch, _CHIP_FRAME, _working(pty))
    [entry] = _pending(session)
    seen: list = []
    confirm = type(session)._confirm_typeahead_submit

    def _discard_mid_confirm(self, *a, **k):
        seen.append(self.discard_pending_input(entry["id"]))
        return confirm(self, *a, **k)

    monkeypatch.setattr(type(session), "_confirm_typeahead_submit", _discard_mid_confirm)
    session._run_deferred_submit(*args, **kw)

    [(status, body)] = seen
    assert body["reason"] != "discarding"
    assert (status, body["reason"]) == (409, "not_confirmed")
    assert [e["state"] for e in _pending(session)] == ["queued"]


def test_a_new_send_waits_while_a_discarded_payload_is_being_cleared(scripted, monkeypatch):
    """A new paste would land behind the discarded one and its Enter would
    submit both, so nothing is written."""
    session, pty, clock = scripted
    pty.paint(_TURN_START_FRAME)
    args, kw = _defer_steer(session, pty, clock, monkeypatch, _CHIP_FRAME, _working(pty))
    [entry] = _pending(session)
    session.discard_pending_input(entry["id"])
    start = len(pty.writes)

    outcome = session.submit_input(_SHORT, True)

    assert outcome.reason == INPUT_DISCARD_PENDING
    assert _writes_after(pty, start) == []
    # The watcher was not superseded: it still clears the discarded payload.
    session._run_deferred_submit(*args, **kw)
    assert session.last_input["reason"] == INPUT_DISCARDED


def test_a_payload_pasted_over_somebodys_text_is_not_cleared(scripted, monkeypatch):
    """Ctrl-U would take the half-typed text in front of it too."""
    session, pty, clock = scripted
    session.write("draft")
    pty.paint(_TURN_START_FRAME)
    _defer_steer(session, pty, clock, monkeypatch, _CHIP_FRAME, _working(pty))
    [entry] = _pending(session)
    assert entry["removable"] is False

    status, body = session.discard_pending_input(entry["id"])
    assert (status, body["reason"]) == (409, "composer_has_text")


# ------------------------------------------------------------- the list itself


def test_clear_presses_cover_every_line_with_slack():
    pending = PendingInputs()
    pending.add("one", 0.0, "queued")
    pending.add("two\nlines", 0.0, "queued")
    assert pending.clear_presses(("queued",)) == 2 * 3 + 6


def test_a_composer_needing_more_presses_than_the_cap_is_left_unknown(scripted, monkeypatch):
    session, pty, _clock = scripted
    monkeypatch.setattr(pending_discard, "_CLEAR_MAX_PRESSES", 3)
    entry = _queue_short(session, pty)
    start = len(pty.writes)

    status, _body = session.discard_pending_input(entry["id"])

    assert status == 200
    assert _writes_after(pty, start).count(_CTRL_U) == 3
    assert session.to_api()["composer_dirty"] is None
