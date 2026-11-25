"""The chief's tunable timings: default, range and unit for each.

Each default equals the constant it replaced, so a config that never set one
behaves exactly as before. app-launcher-lite uses the same names, defaults and
ranges, so fleet-config's chief reads one shape from either launcher's
``GET /api/board/chief/settings``. The doorbell reads the re-ring and re-offer
values live; the hold and stall values are read by fleet-config's chief.
"""

from __future__ import annotations

from typing import Dict, NamedTuple


class ChiefTiming(NamedTuple):
    default: int
    minimum: int
    maximum: int
    unit: str


CHIEF_TIMINGS: Dict[str, ChiefTiming] = {
    # Doorbell back-off before it rings an idle chief again.
    "chief_rering_seconds": ChiefTiming(60, 30, 600, "seconds"),
    # An unacknowledged mailbox claim is offered again after this long.
    "chief_reoffer_minutes": ChiefTiming(15, 10, 120, "minutes"),
    # The Stop hook holds the chief's turn end at most once per this window.
    "chief_hold_minutes": ChiefTiming(10, 5, 60, "minutes"),
    # A headless run silent this long is killed by the stall watchdog.
    "chief_stall_minutes": ChiefTiming(45, 15, 240, "minutes"),
}


def check_chief_timings(cfg: object) -> None:
    """Raise ValueError unless every chief timing is a whole number in its range.

    The type test is exact: a JSON ``true`` is an int in Python, and no
    Settings field sends one.
    """
    for name, spec in CHIEF_TIMINGS.items():
        value = getattr(cfg, name)
        if type(value) is not int or not spec.minimum <= value <= spec.maximum:
            raise ValueError(
                f"{name} must be a whole number of {spec.unit} between "
                f"{spec.minimum} and {spec.maximum}; got {value!r}"
            )


def chief_timings_payload(cfg: object) -> Dict[str, int]:
    """The chief timings as stored, keyed by setting name."""
    return {name: getattr(cfg, name) for name in CHIEF_TIMINGS}
