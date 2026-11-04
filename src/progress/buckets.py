"""The date range and the day / week / month buckets of ``GET /api/progress``."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

BUCKETS = ("day", "week", "month")
DEFAULT_RANGE_DAYS = 30
# The hub's own cap on a history range; one cap means the hub never refuses a
# range this route accepted.
MAX_RANGE_DAYS = 400


def today_local() -> date:
    return datetime.now().astimezone().date()


def parse_range(from_: Optional[str], to: Optional[str], *, today: Optional[date] = None) -> Tuple[date, date]:
    """``(first, last)`` day, inclusive; raises :class:`ValueError` with a sentence for the caller."""
    last = _parse_day(to, "to") if to else (today or today_local())
    first = _parse_day(from_, "from") if from_ else last - timedelta(days=DEFAULT_RANGE_DAYS - 1)
    if first > last:
        raise ValueError("from must not be after to")
    if (last - first).days + 1 > MAX_RANGE_DAYS:
        raise ValueError(f"the range is at most {MAX_RANGE_DAYS} days")
    return first, last


def _parse_day(value: str, name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} must be a day as YYYY-MM-DD") from None


def bucket_start(day: date, bucket: str) -> date:
    """The first day of the bucket holding ``day``: the day, its Monday, or the 1st."""
    if bucket == "week":
        return day - timedelta(days=day.weekday())
    if bucket == "month":
        return day.replace(day=1)
    return day


def bucket_end(start: date, bucket: str) -> date:
    if bucket == "week":
        return start + timedelta(days=6)
    if bucket == "month":
        following = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        return following - timedelta(days=1)
    return start


def bucket_plan(first: date, last: date, bucket: str) -> List[Dict[str, object]]:
    """Every bucket the range touches, empty ones included, oldest first.

    ``days`` are only the range's own days: a bucket cut by ``from`` or ``to``
    sums just the part inside the range, though ``start`` / ``end`` name the
    whole bucket.
    """
    plan: List[Dict[str, object]] = []
    day = first
    while day <= last:
        start = bucket_start(day, bucket)
        end = bucket_end(start, bucket)
        inside = [start + timedelta(days=n) for n in range((end - start).days + 1)]
        plan.append({
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": [d.isoformat() for d in inside if first <= d <= last],
        })
        day = end + timedelta(days=1)
    return plan
