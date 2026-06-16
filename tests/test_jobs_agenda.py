"""Unit tests for :func:`src.jobs.upcoming_fires` (issue #230).

Enumerates every fire of a schedule within a window by walking the tested
``next_fire`` forward. Frequent cadences (``minutes`` / ``hourly``) are
summarised by the agenda, not expanded, so they return ``[]`` here.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from src.jobs import upcoming_fires
from src.jobs_config import Schedule


# Tuesday 2026-06-16 10:00:00.
START = datetime(2026, 6, 16, 10, 0, 0)
WEEK = START + timedelta(days=7)


def test_daily_yields_one_per_day():
    fires = upcoming_fires(Schedule(type="daily", at="06:00"), start=START, end=WEEK)
    # 06:00 already passed today → first is tomorrow, then one per day: 7 total
    # across the 7-day window (17th..23rd).
    assert len(fires) == 7
    assert fires[0] == datetime(2026, 6, 17, 6, 0, 0)
    assert all(b - a == timedelta(days=1) for a, b in zip(fires, fires[1:]))


def test_daily_later_today_is_included():
    fires = upcoming_fires(Schedule(type="daily", at="18:00"), start=START, end=WEEK)
    assert fires[0] == datetime(2026, 6, 16, 18, 0, 0)
    assert len(fires) == 7


def test_daily_times_expands_every_slot():
    sched = Schedule(type="daily_times", at=["06:15", "12:00", "18:00"])
    fires = upcoming_fires(sched, start=START, end=WEEK)
    # 2 today (06:15 passed) + 3/day for the six full days + the final 06:15
    # on day 7 before the window closes at Tue 10:00 = 21.
    assert len(fires) == 2 + 3 * 6 + 1
    assert fires[0] == datetime(2026, 6, 16, 12, 0, 0)
    assert fires[-1] == datetime(2026, 6, 23, 6, 15, 0)
    assert fires == sorted(fires)


def test_weekly_yields_single_occurrence_in_a_week():
    sched = Schedule(type="weekly", day="FRI", at="01:30")
    fires = upcoming_fires(sched, start=START, end=WEEK)
    assert fires == [datetime(2026, 6, 19, 1, 30, 0)]


def test_once_inside_window():
    sched = Schedule(type="once", at="2026-06-20T14:30")
    assert upcoming_fires(sched, start=START, end=WEEK) == [
        datetime(2026, 6, 20, 14, 30, 0)
    ]


def test_once_outside_window_is_empty():
    sched = Schedule(type="once", at="2026-07-01T14:30")
    assert upcoming_fires(sched, start=START, end=WEEK) == []


def test_frequent_types_are_not_enumerated():
    assert upcoming_fires(Schedule(type="minutes", every=5), start=START, end=WEEK) == []
    assert upcoming_fires(Schedule(type="hourly", every=2), start=START, end=WEEK) == []


def test_none_is_empty():
    assert upcoming_fires(Schedule(type="none"), start=START, end=WEEK) == []


def test_cap_bounds_the_list():
    # A 1-day window of a single daily slot can't exceed the cap; use a tiny
    # cap to prove the guard fires rather than running unbounded.
    sched = Schedule(type="daily_times", at=["00:00", "06:00", "12:00", "18:00"])
    fires = upcoming_fires(sched, start=START, end=START + timedelta(days=30), cap=5)
    assert len(fires) == 5
