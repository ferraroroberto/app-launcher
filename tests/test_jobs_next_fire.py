"""Unit tests for :func:`src.jobs.next_fire` (issue #229).

Computes the next wall-clock fire from a :class:`~src.jobs_config.Schedule`
definition — deterministic, no schtasks. ``now`` is injected so every case
is reproducible. This is the field the Jobs tab sorts on and renders the
"in 3h" countdown from.
"""

from __future__ import annotations

from datetime import datetime

from src.jobs import next_fire
from src.jobs_config import Schedule


# A fixed reference: Tuesday 2026-06-16 10:00:00 (weekday()==1).
NOW = datetime(2026, 6, 16, 10, 0, 0)


def test_none_has_no_next_fire():
    assert next_fire(Schedule(type="none"), now=NOW) is None


def test_minutes_projects_forward():
    assert next_fire(Schedule(type="minutes", every=5), now=NOW) == datetime(
        2026, 6, 16, 10, 5, 0
    )


def test_hourly_projects_forward():
    assert next_fire(Schedule(type="hourly", every=2), now=NOW) == datetime(
        2026, 6, 16, 12, 0, 0
    )


def test_daily_later_today():
    assert next_fire(Schedule(type="daily", at="18:00"), now=NOW) == datetime(
        2026, 6, 16, 18, 0, 0
    )


def test_daily_already_passed_rolls_to_tomorrow():
    assert next_fire(Schedule(type="daily", at="06:00"), now=NOW) == datetime(
        2026, 6, 17, 6, 0, 0
    )


def test_daily_times_picks_earliest_upcoming_slot():
    # 06:15 already passed, 12:00 + 18:00 still ahead → 12:00 is next.
    sched = Schedule(type="daily_times", at=["06:15", "12:00", "18:00"])
    assert next_fire(sched, now=NOW) == datetime(2026, 6, 16, 12, 0, 0)


def test_daily_times_all_passed_rolls_to_earliest_tomorrow():
    sched = Schedule(type="daily_times", at=["06:15", "08:00"])
    assert next_fire(sched, now=NOW) == datetime(2026, 6, 17, 6, 15, 0)


def test_weekly_future_weekday_this_week():
    # NOW is Tuesday; next FRI 01:30 is three days ahead, same week.
    sched = Schedule(type="weekly", day="FRI", at="01:30")
    assert next_fire(sched, now=NOW) == datetime(2026, 6, 19, 1, 30, 0)


def test_weekly_same_day_time_passed_rolls_a_week():
    # NOW is Tuesday 10:00; a Tuesday 06:00 schedule already fired today.
    sched = Schedule(type="weekly", day="TUE", at="06:00")
    assert next_fire(sched, now=NOW) == datetime(2026, 6, 23, 6, 0, 0)


def test_weekly_same_day_time_ahead_is_today():
    sched = Schedule(type="weekly", day="TUE", at="21:00")
    assert next_fire(sched, now=NOW) == datetime(2026, 6, 16, 21, 0, 0)


def test_once_future_returns_instant():
    sched = Schedule(type="once", at="2026-06-20T14:30")
    assert next_fire(sched, now=NOW) == datetime(2026, 6, 20, 14, 30, 0)


def test_once_elapsed_returns_none():
    sched = Schedule(type="once", at="2026-06-10T14:30")
    assert next_fire(sched, now=NOW) is None


def test_paused_job_active_schedule_is_none():
    # A paused job's active schedule is parked as none → no next fire,
    # even though paused_schedule still carries the real weekly shape.
    assert next_fire(Schedule(type="none"), now=NOW) is None
