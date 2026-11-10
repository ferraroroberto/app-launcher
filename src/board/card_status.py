"""The Board session card's status set, declared once.

A card's ``status`` is exactly one of the nine values below. Every grouping a
consumer needs (the Your-turn lane, the chief's resting label, the doorbell's
safe-to-type set, the plan-picker screen check) is derived from the
properties declared here, so a status is added or regrouped in one place.
``app/webapp/static/glance.js`` keeps its own label table and chief set;
``tests/unit/board/test_card_status_contract.py`` fails when either drifts
from this declaration.

The hook writer stamps a smaller raw vocabulary (:data:`HOOK_STAMPS`). Its
``needs-you`` is always split into a card value and never reaches a card.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet

WORKING = "working"
UNKNOWN = "unknown"
IDLE = "idle"
WAITING_ON_TASK = "waiting-on-task"
STALLED = "stalled"
AWAITING_DECISION = "awaiting-decision"
TOOL_PENDING = "tool-pending"
IDLE_FINISHED = "idle-finished"
AWAITING_INPUT = "awaiting-input"

#: The raw hook stamp that the needs-you split turns into a card value.
NEEDS_YOU_STAMP = "needs-you"

#: Raw stamps the hook writer emits. Any other raw value renders ``unknown``.
HOOK_STAMPS: FrozenSet[str] = frozenset({WORKING, NEEDS_YOU_STAMP, IDLE})

LANE_YOUR_TURN = "your_turn"
LANE_CLAUDE_TURN = "claude_turn"


@dataclass(frozen=True)
class CardStatus:
    """One card value and the properties every consumer groups it by.

    ``needs_human``: a human must look at this terminal, so a worker's card
    routes to Your turn. A clean stop, an ordinary pending tool call and a wait
    on the session's own background task are Claude holding a workspace, not
    alerts: a harness notification wakes a session when its background Bash,
    sub-agent or ``Monitor`` finishes, so no human is being waited on.

    ``chief_resting``: the standing chief shows "standing by" instead of the
    status word. The chief's Stop-hook status sits in the needs-you family for
    most of its life, and for a long-lived chat that is a resting state.
    ``stalled`` stays visible because a chief dispatch stuck that long is a
    real anomaly; ``waiting-on-task`` keeps its own, more telling word.

    ``doorbell_idle``: the chief doorbell may type into the chief now: it sits
    at its prompt with nothing pending on screen. This differs from
    ``chief_resting`` on purpose. A decision picker is on screen, so typed text
    would answer it; a background waiter leaves the composer free, so a typed
    line starts a new turn while the waiter keeps running. ``stalled`` rings
    too: the chief's own fallback waiter outlives the dispatch threshold by
    design, and the screen check keeps a picker from being typed into.

    ``screen_checked``: the needs-you split found no pending decision, but
    Claude can keep a pending ``ExitPlanMode`` call off disk while its picker
    is up, so a live launcher PTY's screen is read for the picker.

    ``turn_ended``: the agent's turn is over, whatever it now waits on. Only
    ``working`` (a turn in flight) and ``unknown`` (no evidence either way)
    lack it.

    ``quit_safe``: a graceful quit loses nothing, because the turn ended clean
    with nothing running and nothing pending on screen. ``awaiting-input`` is
    left out: it is a clean stop that a repo with an open issue workflow
    downgrades because the work may not be finished.
    """

    value: str
    needs_human: bool = False
    chief_resting: bool = False
    doorbell_idle: bool = False
    screen_checked: bool = False
    turn_ended: bool = False
    quit_safe: bool = False

    @property
    def lane(self) -> str:
        """The Board column a worker's card routes to."""
        return LANE_YOUR_TURN if self.needs_human else LANE_CLAUDE_TURN


CARD_STATUSES: Dict[str, CardStatus] = {
    s.value: s
    for s in (
        CardStatus(WORKING),
        CardStatus(UNKNOWN),
        CardStatus(IDLE, chief_resting=True, doorbell_idle=True, turn_ended=True, quit_safe=True),
        CardStatus(WAITING_ON_TASK, doorbell_idle=True, turn_ended=True),
        CardStatus(
            STALLED, needs_human=True, doorbell_idle=True, screen_checked=True, turn_ended=True,
        ),
        CardStatus(AWAITING_DECISION, needs_human=True, chief_resting=True, turn_ended=True),
        CardStatus(TOOL_PENDING, screen_checked=True, turn_ended=True),
        CardStatus(
            IDLE_FINISHED, chief_resting=True, doorbell_idle=True, screen_checked=True,
            turn_ended=True, quit_safe=True,
        ),
        CardStatus(
            AWAITING_INPUT, needs_human=True, chief_resting=True,
            doorbell_idle=True, screen_checked=True, turn_ended=True,
        ),
    )
}


def _where(prop: str) -> FrozenSet[str]:
    return frozenset(v for v, s in CARD_STATUSES.items() if getattr(s, prop))


#: Card values whose worker card routes to Your turn.
NEEDS_HUMAN: FrozenSet[str] = _where("needs_human")
#: Card values the standing chief labels "standing by".
CHIEF_RESTING: FrozenSet[str] = _where("chief_resting")
#: Card values the chief doorbell may type into.
DOORBELL_IDLE: FrozenSet[str] = _where("doorbell_idle")
#: Card values a live launcher PTY's plan picker can turn into ``awaiting-decision``.
SCREEN_CHECKED: FrozenSet[str] = _where("screen_checked")
#: Card values whose agent turn is over.
TURN_ENDED: FrozenSet[str] = _where("turn_ended")
#: Card values a graceful quit can close without losing anything.
QUIT_SAFE: FrozenSet[str] = _where("quit_safe")
