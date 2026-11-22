"""Request timing: queue vs worker time, the budget table and the in-flight peaks.

Timing is proven with a fake clock and an event-gated one-thread pool, never a
sleep: the clock only moves when the test moves it.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from src import request_timing as rt


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(rt, "time", SimpleNamespace(monotonic=fake.monotonic))
    return fake


def test_queue_and_worker_time_are_told_apart(clock):
    pool = rt.TimedExecutor("probe", max_workers=1)
    timing = rt.RequestTiming()
    first_running, release = threading.Event(), threading.Event()

    def first() -> None:
        first_running.set()
        release.wait(timeout=5)

    token = rt.current.set(timing)
    try:
        a = pool.submit(first)
        b = pool.submit(lambda: "done")  # waits behind the only thread
    finally:
        rt.current.reset(token)
    assert first_running.wait(timeout=5)
    clock.now = 5.0
    release.set()
    a.result(timeout=5)
    assert b.result(timeout=5) == "done"
    pool.shutdown()

    assert timing.queue_s == pytest.approx(5.0)  # b queued 5 s; a queued none
    assert timing.worker_s == pytest.approx(5.0)  # a ran 5 s; b ran none
    assert timing.calls == 2
    assert timing.pool == "probe"


def test_a_submit_with_no_request_is_not_timed():
    pool = rt.TimedExecutor("probe", max_workers=1)
    assert pool.submit(lambda: 7).result(timeout=5) == 7
    pool.shutdown()
    assert rt.current.get() is None


def test_server_timing_and_log_fields_name_the_pool():
    timing = rt.RequestTiming()
    timing.record("hot-poll", 0.25, 0.5)
    timing.record("default", 0.0, 0.1)
    header = timing.server_timing(1.0)
    assert 'queue;dur=250.0;desc="default+hot-poll"' in header
    assert 'worker;dur=600.0;desc="default+hot-poll"' in header
    assert header.endswith("app;dur=1000.0")
    assert timing.log_fields(1.0) == "queue_ms=250 worker_ms=600 total_ms=1000 pool=default+hot-poll"


def test_a_request_that_used_no_thread_says_so():
    assert rt.RequestTiming().pool == rt.NO_POOL


@pytest.mark.parametrize(
    ("method", "path", "query", "expected"),
    [
        ("GET", "/api/board", "", "board"),
        ("GET", "/api/claude-code/sessions", "", "session-list"),
        ("GET", "/api/claude-code/sessions/abc/transcript", "after=10&size=10", "transcript-tick"),
        ("GET", "/api/claude-code/sessions/abc/transcript", "limit=20", "transcript-open"),
        ("POST", "/api/claude-code/sessions/abc/input", "", "send"),
        ("GET", "/api/claude-code/git-status", "fresh=true", "git-status"),
        ("GET", "/", "", "shell"),
        ("GET", "/api/jobs", "", "jobs-list"),
        ("POST", "/api/board", "", None),
        ("GET", "/api/version", "", None),
    ],
)
def test_classify(method, path, query, expected):
    assert rt.classify(method, path, query) == expected


def test_the_issue_budgets_are_the_defaults():
    table = rt.default_budgets()
    assert table["transcript-tick"] == rt.Budget(0.5, 1.5)
    assert table["session-list"] == rt.Budget(0.5, 1.5)
    assert table["board"] == rt.Budget(0.5, 1.5)
    assert table["send"] == rt.Budget(5.0, 10.0)
    assert table["git-status"] == rt.Budget(5.0, 10.0)
    assert table["transcript-open"].soft_s == 1.0


def test_verdict_has_three_states():
    budget = rt.Budget(0.5, 1.5)
    assert rt.budget_verdict(budget, 0.5) == "ok"
    assert rt.budget_verdict(budget, 0.9) == "over"
    assert rt.budget_verdict(budget, 1.6) == "breach"


def test_overrides_replace_a_class_and_bad_ones_are_dropped():
    kept, dropped = rt.clean_overrides(
        {"board": [1, 2], "nope": [1, 2], "send": [3, 1], "git-status": "x"}
    )
    assert kept == {"board": [1.0, 2.0]}
    assert len(dropped) == 3
    table = rt.resolve_budgets({"board": [1, 2], "send": [9, 1]})
    assert table["board"] == rt.Budget(1.0, 2.0)
    assert table["send"] == rt.Budget(5.0, 10.0)  # an invalid pair never switches a budget off


def test_overrides_that_are_not_an_object_are_dropped_whole():
    assert rt.clean_overrides(None) == ({}, [])
    kept, dropped = rt.clean_overrides([1, 2])
    assert kept == {} and dropped


def test_inflight_peak_is_windowed():
    now = [0.0]
    stats = rt.RequestStats(clock=lambda: now[0])
    stats.entered(3)
    stats.entered(7)
    stats.left(0)
    now[0] = 500.0
    stats.entered(2)
    assert stats.max_inflight(window_s=60) == 2
    assert stats.max_inflight(window_s=900) == 7


def test_stats_count_verdicts_per_class():
    stats = rt.RequestStats(clock=lambda: 0.0)
    stats.judged("board", "ok")
    stats.judged("board", "breach")
    assert stats.snapshot(10)["verdicts"]["board"] == {"ok": 1, "over": 0, "breach": 1}


def test_the_default_executor_stand_in_is_charged_to_the_default_pool():
    import asyncio

    timing = rt.RequestTiming()

    async def work() -> None:
        asyncio.get_running_loop().set_default_executor(rt.default_executor())
        token = rt.current.set(timing)
        try:
            await asyncio.to_thread(lambda: None)
        finally:
            rt.current.reset(token)

    asyncio.run(work())
    assert timing.pool == rt.DEFAULT_POOL and timing.calls == 1
