"""The quiet window the night auto-restart may act in.

A window is two ``HH:MM`` local times and may cross midnight (21:00 to 05:00).
Pure functions on a ``datetime`` so a test drives them with a fake clock; kept
apart from the restart logic because ``webapp_config`` validates a saved window
with it and must not import the module that reads the config.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

DEFAULT_START = "21:00"
DEFAULT_END = "05:00"


def minutes_of(text: object) -> Optional[int]:
    """Minutes after midnight for an ``HH:MM`` string, else None."""
    found = _HHMM.match(text) if isinstance(text, str) else None
    return int(found.group(1)) * 60 + int(found.group(2)) if found else None


def validate_window(start: object, end: object) -> None:
    """Raise ValueError unless both are ``HH:MM`` and differ (equal would mean no
    window, or all day: neither is a quiet window)."""
    for name, value in (("process_auto_restart_start", start), ("process_auto_restart_end", end)):
        if minutes_of(value) is None:
            raise ValueError(f"{name} must be a time as HH:MM (24 hours); got {value!r}")
    if start == end:
        raise ValueError("the auto-restart window must start and end at different times")


def in_window(now: datetime, start: str, end: str) -> bool:
    """Is *now* inside the window ``[start, end)``? Crossing midnight when start > end."""
    lo, hi, at = minutes_of(start), minutes_of(end), now.hour * 60 + now.minute
    if lo is None or hi is None or lo == hi:
        return False
    return lo <= at < hi if lo < hi else (at >= lo or at < hi)


def night_of(now: datetime, start: str, end: str) -> date:
    """The date the window containing *now* began on, so 02:00 belongs to the
    previous evening's night. Only meaningful while :func:`in_window` is true."""
    lo, hi = minutes_of(start), minutes_of(end)
    after_midnight = lo is not None and hi is not None and lo > hi and now.hour * 60 + now.minute < hi
    return (now - timedelta(days=1)).date() if after_midnight else now.date()
