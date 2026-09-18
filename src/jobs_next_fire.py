"""When a job fires next - pure schedule arithmetic, no Windows in sight.

Split out of ``src/jobs_schtasks.py`` (a ``/codebase-audit`` maintainability
finding, #1006). It lived there for a reason that stopped applying: it
exists *because* schtasks' own "Next Run Time" string is locale-formatted
and useless to sort by, so proximity to the parser was once the point. But
nothing here touches schtasks - no subprocess, no CLI parsing, no TTL cache,
no Windows at all. Everything is derived from the bounded
:class:`~src.jobs_config.Schedule` shape, which made a reader of
``jobs_coverage.py`` chase ``upcoming_fires`` into a 1000-line Task
Scheduler client to find pure date arithmetic, and a reader of that client
carry 100 lines of calendar maths that could not affect it.

The contract, which is the whole reason this is separable: **pure and
deterministic**. Same ``Schedule`` and same ``now`` give the same answer, on
any machine, with no Task Scheduler installed. Keep it that way - a
schtasks import here would put the seam back.

Callers reach these through ``src.jobs``'s re-export facade, which is why
this move changed no call site outside the two modules that import them
directly.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, List, Optional, Tuple

from src.jobs_config import Schedule

# The schtasks "Next Run Time" string above is a locale-formatted, lexically
# sorted best-effort value — fine to *display*, useless to *sort by* or to
# turn into a countdown. The schedule definition, however, is a small
# deterministic set (see src.jobs_config), so we compute the next wall-clock
# fire ourselves. This is the field the UI sorts on and renders "in 3h" from.

# Day-name → datetime.weekday() index (Mon=0 .. Sun=6).
_WEEKDAY_INDEX = {
    "MON": 0,
    "TUE": 1,
    "WED": 2,
    "THU": 3,
    "FRI": 4,
    "SAT": 5,
    "SUN": 6,
}


def _hhmm(value: Any) -> Optional[Tuple[int, int]]:
    """Parse ``"HH:MM"`` → ``(hour, minute)``, or ``None`` when malformed."""
    if not isinstance(value, str):
        return None
    try:
        hh, mm = value.split(":", 1)
        h, m = int(hh), int(mm)
    except ValueError:
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return h, m
    return None


def next_fire(
    sched: Schedule, *, now: Optional[datetime] = None
) -> Optional[datetime]:
    """The next wall-clock fire time for ``sched``, computed from its shape.

    Pure + deterministic — derived from the bounded schedule definition,
    not from schtasks. Returns ``None`` for ``none`` (which includes a
    *paused* job, whose active schedule is parked as ``none`` while the
    real shape lives in ``paused_schedule``) and for a ``once`` schedule
    that has already elapsed. ``now`` is injectable for testing.

    Computed in local naive time: the launcher and Task Scheduler both run
    in the logged-on session's local time, so this matches what the user
    sees and what actually fires.
    """
    now = now or datetime.now()
    t = sched.type
    if t == "minutes" and isinstance(sched.every, int) and sched.every > 0:
        return now + timedelta(minutes=sched.every)
    if t == "hourly" and isinstance(sched.every, int) and sched.every > 0:
        return now + timedelta(hours=sched.every)
    if t == "daily":
        hm = _hhmm(sched.at)
        if hm is None:
            return None
        candidate = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    if t == "daily_times" and isinstance(sched.at, list):
        best: Optional[datetime] = None
        for entry in sched.at:
            hm = _hhmm(entry)
            if hm is None:
                continue
            candidate = now.replace(
                hour=hm[0], minute=hm[1], second=0, microsecond=0
            )
            if candidate <= now:
                candidate += timedelta(days=1)
            if best is None or candidate < best:
                best = candidate
        return best
    if t == "weekly" and sched.day in _WEEKDAY_INDEX:
        hm = _hhmm(sched.at)
        if hm is None:
            return None
        candidate = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
        days_ahead = (_WEEKDAY_INDEX[sched.day] - now.weekday()) % 7
        candidate += timedelta(days=days_ahead)
        if candidate <= now:
            candidate += timedelta(days=7)
        return candidate
    if t == "once" and isinstance(sched.at, str):
        try:
            fire = datetime.fromisoformat(sched.at)
        except ValueError:
            return None
        return fire if fire > now else None
    # "none" and any malformed shape fall through to no next fire.
    return None


# Schedule types whose cadence is too dense to enumerate over an agenda
# window (issue #230). The agenda summarises these as a single "frequent"
# row instead of one entry per fire. next_fire never returns None for
# them, so upcoming_fires must short-circuit before the enumeration loop.
FREQUENT_SCHEDULE_TYPES = frozenset({"minutes", "hourly"})


def upcoming_fires(
    sched: Schedule, *, start: datetime, end: datetime, cap: int = 200
) -> List[datetime]:
    """Every fire of ``sched`` in the half-open window ``[start, end)``.

    Built by walking :func:`next_fire` forward — each call with
    ``now=cursor`` returns a fire strictly after ``cursor`` (the
    ``candidate <= now`` roll-forward guarantees it), so advancing the
    cursor to each result enumerates the window without re-deriving any
    cadence math (issue #230 reuses #229's tested helper).

    Returns ``[]`` for ``none`` / already-elapsed ``once`` and for the
    dense :data:`FREQUENT_SCHEDULE_TYPES` (``minutes`` / ``hourly``),
    which the agenda summarises rather than expands. ``cap`` bounds the
    list defensively (a 3-slot ``daily_times`` over a week is ~21).
    """
    if sched.type in FREQUENT_SCHEDULE_TYPES:
        return []
    fires: List[datetime] = []
    cursor = start
    while len(fires) < cap:
        nf = next_fire(sched, now=cursor)
        if nf is None or nf >= end:
            break
        fires.append(nf)
        cursor = nf
    return fires
