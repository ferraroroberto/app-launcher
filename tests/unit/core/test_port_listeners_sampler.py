"""The Apps tab's listener list reads from a background sampler.

The scan walks every socket and asks each owner for its cwd and command line,
which takes seconds on a slow box. These tests pin that a read never waits for
it, that a scan past its budget reads unknown with a reason (never an empty
list), that concurrent reads start at most one scan, and that nobody watching
means nobody scanning. The clock is fake and the slow scanner is a thread held
by an event, so nothing here depends on how fast the host is.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from src.diagnostics import PortOwner
from src.ports.listeners import ListenerSampler
from src.request_timing import TimedExecutor


class _Clock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _Scanner:
    """A scan that holds until released, and counts how often it started."""

    def __init__(self, owners=None, hold: bool = False) -> None:
        self.owners = owners if owners is not None else [PortOwner(pid=11, port=8501, name="python.exe")]
        self.hold = hold
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self.fail = False

    def __call__(self):
        self.calls += 1
        self.started.set()
        if self.hold:
            assert self.release.wait(10.0), "the test never released the scan"
        if self.fail:
            raise OSError("socket table unreadable")
        return list(self.owners)


@pytest.fixture
def pool():
    executor = TimedExecutor("test-port-scan", max_workers=1)
    yield executor
    executor.shutdown(wait=True, cancel_futures=True)


@pytest.fixture
def clock():
    return _Clock()


def _sampler(scanner, pool, clock) -> ListenerSampler:
    return ListenerSampler(scanner, pool=pool, clock=clock)


async def _scanned(sampler: ListenerSampler) -> None:
    """Let the scan the last read started finish and publish."""
    await sampler.tick_once()


async def test_a_fresh_sampler_reads_unknown_warming_never_an_empty_list(pool, clock):
    scanner = _Scanner(hold=True)
    sampler = _sampler(scanner, pool, clock)
    try:
        reading = sampler.read()
        assert reading.state == "unknown"
        assert reading.reason == "warming"
        assert reading.owners is None
    finally:
        scanner.release.set()
        await _scanned(sampler)


async def test_the_first_read_starts_a_scan_and_the_next_one_has_the_list(pool, clock):
    scanner = _Scanner()
    sampler = _sampler(scanner, pool, clock)
    assert sampler.read().reason == "warming"
    await _scanned(sampler)

    reading = sampler.read()
    assert reading.state == "ok"
    assert [o.port for o in reading.owners] == [8501]
    assert reading.age_seconds == 0.0
    assert scanner.calls == 1


async def test_a_read_returns_the_cached_list_without_waiting_for_a_slow_scan(pool, clock):
    scanner = _Scanner()
    sampler = _sampler(scanner, pool, clock)
    sampler.read()
    await _scanned(sampler)

    # Time passes; the next scan is slow and stays running.
    scanner.hold = True
    scanner.started.clear()
    clock.now += sampler.tick_s + 1.0
    tick = asyncio.create_task(sampler.tick_once())
    for _ in range(500):
        if scanner.started.is_set():
            break
        await asyncio.sleep(0.01)
    assert scanner.started.is_set()

    reading = sampler.read()  # returns at once, though the scan is still holding
    assert not scanner.release.is_set() and not tick.done()
    assert reading.state == "ok"
    assert [o.port for o in reading.owners] == [8501]
    assert reading.age_seconds == sampler.tick_s + 1.0

    scanner.release.set()
    await tick


async def test_a_scan_past_its_budget_reads_unknown_with_its_reason(pool, clock):
    scanner = _Scanner(hold=True)
    sampler = _sampler(scanner, pool, clock)
    sampler.read()
    clock.now += sampler.budget_s + 1.0

    reading = sampler.read()
    assert reading.state == "unknown"
    assert reading.reason == "overrun"
    assert "budget" in reading.detail
    assert reading.owners is None  # never an empty list

    scanner.release.set()
    await _scanned(sampler)
    assert sampler.read().state == "ok"


async def test_an_overrun_hides_a_list_the_scan_can_no_longer_refresh(pool, clock):
    scanner = _Scanner()
    sampler = _sampler(scanner, pool, clock)
    sampler.read()
    await _scanned(sampler)

    scanner.hold = True
    clock.now += sampler.tick_s + 1.0
    tick = asyncio.create_task(sampler.tick_once())
    for _ in range(500):
        if sampler._inflight is not None:
            break
        await asyncio.sleep(0.01)
    clock.now += sampler.budget_s + 1.0

    reading = sampler.read()
    assert (reading.state, reading.reason, reading.owners) == ("unknown", "overrun", None)

    scanner.release.set()
    await tick


async def test_concurrent_reads_and_ticks_start_at_most_one_scan(pool, clock):
    scanner = _Scanner(hold=True)
    sampler = _sampler(scanner, pool, clock)
    readings = [sampler.read() for _ in range(5)]
    ticks = [asyncio.create_task(sampler.tick_once()) for _ in range(3)]
    await asyncio.sleep(0.05)
    clock.now += sampler.tick_s + 1.0
    readings += [sampler.read() for _ in range(5)]
    ticks.append(asyncio.create_task(sampler.tick_once()))
    await asyncio.sleep(0.05)

    scanner.release.set()
    await asyncio.gather(*ticks)
    assert scanner.calls == 1
    assert all(r.state == "unknown" for r in readings)


async def test_nobody_watching_means_no_scan(pool, clock):
    scanner = _Scanner()
    sampler = _sampler(scanner, pool, clock)
    await sampler.tick_once()
    assert scanner.calls == 0

    sampler.read()
    await _scanned(sampler)
    assert scanner.calls == 1

    # The demand window lapses: the tick stops scanning until a read arrives.
    clock.now += sampler.demand_s + sampler.tick_s + 1.0
    await sampler.tick_once()
    assert scanner.calls == 1


async def test_the_tick_refreshes_a_watched_list_every_tick_not_every_wake(pool, clock):
    scanner = _Scanner()
    sampler = _sampler(scanner, pool, clock)
    sampler.read()
    await _scanned(sampler)

    clock.now += sampler.tick_s - 1.0
    sampler.read()
    await sampler.tick_once()
    assert scanner.calls == 1  # not due yet

    clock.now += 1.5
    sampler.read()
    await sampler.tick_once()
    assert scanner.calls == 2


async def test_a_failed_scan_reads_unknown_error_and_recovers(pool, clock):
    scanner = _Scanner()
    scanner.fail = True
    sampler = _sampler(scanner, pool, clock)
    sampler.read()
    await _scanned(sampler)

    reading = sampler.read()
    assert (reading.state, reading.reason, reading.owners) == ("unknown", "error", None)
    assert "socket table unreadable" in reading.detail

    scanner.fail = False
    clock.now += sampler.tick_s + 1.0
    await sampler.tick_once()
    assert sampler.read().state == "ok"


async def test_an_old_list_reads_stale_not_current(pool, clock):
    scanner = _Scanner()
    sampler = _sampler(scanner, pool, clock)
    sampler.read()
    await _scanned(sampler)

    scanner.hold = True
    clock.now += sampler.max_age_s + 1.0
    reading = sampler.read()  # starts the replacing scan, which holds
    assert (reading.state, reading.reason, reading.owners) == ("unknown", "stale", None)
    assert reading.age_seconds == sampler.max_age_s + 1.0

    scanner.release.set()
    await _scanned(sampler)
    assert sampler.read().state == "ok"


async def test_a_stopped_port_is_not_served_from_an_earlier_scan(pool, clock):
    scanner = _Scanner()
    sampler = _sampler(scanner, pool, clock)
    sampler.read()
    await _scanned(sampler)
    assert sampler.read().state == "ok"

    sampler.mark_dirty()
    clock.now += 0.5
    scanner.owners = []  # the stopped process is gone from the next scan
    reading = sampler.read()  # starts the rescan at once, not a tick later
    assert (reading.state, reading.reason, reading.owners) == ("unknown", "refreshing", None)
    await _scanned(sampler)

    after = sampler.read()
    assert after.state == "ok"
    assert after.owners == ()
    assert scanner.calls == 2
