"""Tray health watchdog (issue #386) — edge-triggered alert semantics."""

from __future__ import annotations

from typing import Iterator, List

from app.tray.watchdog import HealthWatchdog


def _watchdog(results: Iterator[bool], failures_to_alert: int = 3):
    wedges: List[int] = []
    recoveries: List[bool] = []
    wd = HealthWatchdog(
        probe=lambda: next(results),
        on_wedge=wedges.append,
        on_recover=lambda: recoveries.append(True),
        failures_to_alert=failures_to_alert,
    )
    return wd, wedges, recoveries


def test_alert_fires_once_after_consecutive_failures():
    wd, wedges, recoveries = _watchdog(iter([True, False, False, False, False]))
    for _ in range(5):
        wd.tick()
    assert wedges == [3]  # fired exactly once, at the third failure
    assert recoveries == []


def test_flapping_below_threshold_never_alerts():
    wd, wedges, recoveries = _watchdog(
        iter([False, False, True, False, False, True])
    )
    for _ in range(6):
        wd.tick()
    assert wedges == []
    assert recoveries == []


def test_recovery_fires_once_and_rearms():
    wd, wedges, recoveries = _watchdog(
        iter([False, False, False, True, True, False, False, False])
    )
    for _ in range(8):
        wd.tick()
    assert wedges == [3, 3]  # re-armed by the recovery, alerted again
    assert recoveries == [True]  # only the first success after an alert


def test_probe_exception_counts_as_failure():
    def _boom() -> bool:
        raise RuntimeError("probe died")

    wedges: List[int] = []
    wd = HealthWatchdog(
        probe=_boom,
        on_wedge=wedges.append,
        on_recover=lambda: None,
        failures_to_alert=2,
    )
    assert wd.tick() is False
    assert wd.tick() is False
    assert wedges == [2]
