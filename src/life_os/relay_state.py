"""The state every Life OS relay reply names, declared once.

A relay-enabled Telegram bot calls the launcher's relay routes
(``app/webapp/routers/life_os_relay.py``) through ``scripts/telegram_relay.py``,
and every reply, success or refusal, names a ``state`` so the bot never folds a
failure into "ok". This module is the closed set the routes answer with;
the CLI keeps a stdlib copy (it imports nothing from the repo) that a test
holds equal to this one.

A steer is the one state derived from facts rather than picked per branch:

* ``queued``: the target was mid-turn, so the launcher held the message and
  presses Enter when the turn settles; the reply the bot reads next is the
  turn it was already on, not an answer;
* ``delivered``: the session host confirmed the submit;
* ``unconfirmed``: sent, but nothing verified it.

A held message is ``queued`` whatever the submit fields say: reporting it
``delivered`` would make the bot pass the previous turn's reply off as the
answer. ``docs/life-os-relay-state.md`` has the table.
"""

from __future__ import annotations

from enum import StrEnum


class RelayState(StrEnum):
    """Every value a relay reply's ``state`` may hold."""

    # Outcomes: the call did what it was asked.
    FOUND = "found"
    NONE = "none"
    RESUMED = "resumed"
    OPENED = "opened"
    STARTED = "started"
    ALREADY_OPEN = "already_open"
    DELIVERED = "delivered"
    QUEUED = "queued"
    UNCONFIRMED = "unconfirmed"
    REPLY = "reply"
    STOPPED = "stopped"
    OPEN = "open"
    # Not an answer: the reply could not be read (the reply carries a ``reason``).
    UNAVAILABLE = "unavailable"
    # Refusals: nothing was done, or what happened is not known.
    REFUSED = "refused"
    RELAY_OFF = "relay_off"
    NOT_FOUND = "not_found"
    NOT_THIS_SKILL = "not_this_skill"
    NOT_RESUMABLE = "not_resumable"
    HOST_UNREACHABLE = "host_unreachable"
    STRANDED = "stranded"
    GONE = "gone"
    UNKNOWN = "unknown"
    NOT_RELAY_OWNED = "not_relay_owned"
    BUSY = "busy"
    CAPTURE_UNCONFIRMED = "capture_unconfirmed"
    STOP_UNCONFIRMED = "stop_unconfirmed"


#: The states the CLI exits 0 on: the call did what it was asked.
OUTCOMES = frozenset({
    RelayState.FOUND, RelayState.NONE, RelayState.RESUMED, RelayState.OPENED,
    RelayState.STARTED, RelayState.ALREADY_OPEN, RelayState.DELIVERED, RelayState.QUEUED,
    RelayState.UNCONFIRMED, RelayState.REPLY, RelayState.STOPPED, RelayState.OPEN,
})


def steer_state(deferred: bool, confirmed: bool) -> RelayState:
    """A steer's state: ``deferred`` when the host held it mid-turn, ``confirmed`` when it verified the submit."""
    if deferred:
        return RelayState.QUEUED
    if confirmed:
        return RelayState.DELIVERED
    return RelayState.UNCONFIRMED
