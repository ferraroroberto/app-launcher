"""``glance.js``'s status tables match the declared card-status set.

The UI falls back to the ``unknown`` label for a status it has no entry for,
so a server-side status missing from ``STATUS_META`` would render blank with
no error anywhere. The chief's "standing by" set is the ``chief_resting``
property, written again in JS.
"""

from __future__ import annotations

import re
from pathlib import Path

from src.board import card_status

_GLANCE = Path(__file__).resolve().parents[3] / "app" / "webapp" / "static" / "glance.js"


def _glance() -> str:
    return _GLANCE.read_text(encoding="utf-8")


def test_status_meta_keys_are_the_declared_card_statuses():
    block = re.search(r"const STATUS_META = \{(.*?)\n\};", _glance(), re.S)
    assert block, "STATUS_META not found in glance.js"
    keys = re.findall(r"^\s*'?([a-z-]+)'?\s*:\s*\{", block.group(1), re.M)
    assert sorted(keys) == sorted(card_status.CARD_STATUSES)


def test_chief_standing_by_is_the_chief_resting_property():
    found = re.search(r"const CHIEF_STANDING_BY_STATUSES = new Set\(\[(.*?)\]\)", _glance())
    assert found, "CHIEF_STANDING_BY_STATUSES not found in glance.js"
    members = set(re.findall(r"'([a-z-]+)'", found.group(1)))
    assert members == card_status.CHIEF_RESTING


def test_a_raw_idle_stamp_is_resting_for_the_chief_and_idle_for_the_doorbell():
    """A raw ``idle`` stamp passes through the derivation as the ``idle`` card
    value; both chief groupings must hold it, or the chief reads as busy."""
    from app.webapp import chief_doorbell

    assert card_status.IDLE in card_status.HOOK_STAMPS
    assert card_status.IDLE in card_status.CHIEF_RESTING
    assert card_status.IDLE in card_status.DOORBELL_IDLE
    assert chief_doorbell.Observation(pending=1, status=card_status.IDLE).idle is True


def test_declared_groupings_are_the_ones_consumers_routed_by():
    assert card_status.NEEDS_HUMAN == {"stalled", "awaiting-decision", "awaiting-input"}
    assert card_status.CHIEF_RESTING == {
        "idle", "idle-finished", "awaiting-input", "awaiting-decision",
    }
    assert card_status.DOORBELL_IDLE == {
        "idle", "idle-finished", "awaiting-input", "waiting-on-task", "stalled",
    }
    assert card_status.SCREEN_CHECKED == {
        "idle-finished", "awaiting-input", "tool-pending", "stalled",
    }
    assert card_status.HOOK_STAMPS == {"working", "needs-you", "idle"}
    assert card_status.NEEDS_YOU_STAMP not in card_status.CARD_STATUSES
    assert {s.lane for s in card_status.CARD_STATUSES.values()} == {
        card_status.LANE_YOUR_TURN, card_status.LANE_CLAUDE_TURN,
    }
