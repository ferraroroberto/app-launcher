"""The page header's CPU reading (#1522): the shared sampler and its route.

Pins the four states the header draws (normal, attention after a sustained
≥70%, danger at ≥90%, unknown as its own state), that the route answers
from the cached sample without touching psutil, that the sampler idles
without demand, and that a process name never carries a path or a command
line. All data is invented: no real process list reaches a test.
"""

from __future__ import annotations

import sys

import pytest

from src import system_load as sl


@pytest.fixture(autouse=True)
def _fresh():
    sl.reset()
    yield
    sl.reset()


class _Box:
    """A fake box: cumulative CPU seconds the test advances by hand."""

    def __init__(self) -> None:
        self.busy = 0.0
        self.total = 0.0
        self.procs: dict = {}
        self.cmdlines: dict = {}

    def run(self, seconds: float, cpu_percent: float, cores: int = 16, procs=None) -> None:
        self.total += seconds * cores
        self.busy += seconds * cores * cpu_percent / 100.0
        for key, (name, used) in (procs or {}).items():
            prev = self.procs.get(key, (name, 0.0))[1]
            self.procs[key] = (name, prev + used * seconds)


@pytest.fixture
def box(monkeypatch):
    fake = _Box()
    monkeypatch.setattr(sl, "_cpu_busy_total", lambda: (fake.busy, fake.total))
    monkeypatch.setattr(sl, "_process_times", lambda: dict(fake.procs))
    monkeypatch.setattr(sl, "_memory", lambda: (48 * 1024**3, 64 * 1024**3))
    monkeypatch.setattr(sl, "_cmdline", lambda pid: fake.cmdlines.get(pid, []))
    monkeypatch.setattr(sl.psutil, "cpu_count", lambda *a, **k: 16)
    return fake


def _drive(box: _Box, percents, start: float = 100.0) -> float:
    """Baseline at ``start``, then one sample per percent, TICK_SECONDS apart."""
    now = start
    assert sl.tick_once(now) is None  # the baseline publishes nothing
    for i, pct in enumerate(percents):
        step = sl.FIRST_WINDOW_SECONDS if i == 0 else sl.TICK_SECONDS
        box.run(step, pct)
        now += step
        assert sl.tick_once(now) is not None
    return now


def test_a_fresh_sampler_reads_unknown_warming_never_zero():
    out = sl.read(now=10.0)
    assert out["level"] == "unknown"
    assert out["reason"] == "warming"
    assert out["cpu_percent"] is None


def test_a_quiet_box_reads_normal_with_its_percent(box):
    now = _drive(box, [42.0])
    out = sl.read(now=now)
    assert out["level"] == "normal"
    assert out["reason"] is None
    assert out["cpu_percent"] == 42.0
    assert out["memory"]["percent"] == 75.0
    assert out["cores"] == 16


def test_attention_needs_thirty_sustained_seconds_at_or_over_seventy(box):
    # ~27 s of load (2 + 5 x 5): not yet sustained.
    now = _drive(box, [75.0] * 6)
    assert sl.read(now=now)["level"] == "normal"
    box.run(sl.TICK_SECONDS, 75.0)
    now += sl.TICK_SECONDS
    sl.tick_once(now)
    assert sl.read(now=now)["level"] == "attention"


def test_one_dip_under_seventy_breaks_the_sustained_run(box):
    now = _drive(box, [80.0] * 4 + [60.0] + [80.0] * 4)
    assert sl.read(now=now)["level"] == "normal"


def test_danger_at_ninety_percent(box):
    now = _drive(box, [95.0])
    assert sl.read(now=now)["level"] == "danger"


def test_a_stale_sample_reads_unknown(box):
    now = _drive(box, [30.0])
    out = sl.read(now=now + sl.MAX_AGE_SECONDS + 1)
    assert out["level"] == "unknown"
    assert out["reason"] == "stale"
    assert out["cpu_percent"] is None


def test_a_failing_sampler_reads_unknown_error(monkeypatch):
    import asyncio

    def boom(now=None):
        raise OSError("probe failed")

    monkeypatch.setattr(sl, "tick_once", boom)
    monkeypatch.setattr(sl, "WAKE_SECONDS", 0.0)
    sl.read()  # demand wakes the tick

    async def one_tick():
        task = asyncio.create_task(sl.tick_forever())
        for _ in range(50):
            await asyncio.sleep(0.01)
            if sl._last_error:
                break
        task.cancel()

    asyncio.run(one_tick())
    out = sl.read()
    assert out["level"] == "unknown"
    assert out["reason"] == "error"


def test_an_idle_gap_rebaselines_and_drops_the_sustained_history(box):
    now = _drive(box, [80.0] * 7)
    assert sl.read(now=now)["level"] == "attention"
    # Demand lapsed: the next tick is far later and only takes a baseline.
    later = now + 10 * sl.TICK_SECONDS
    assert sl.tick_once(later) is None
    assert sl.read(now=later)["reason"] == "warming"


def test_top_three_by_cores_counts_a_process_born_inside_the_window(box):
    box.procs = {(1, 1): ("idle-ish.exe", 0.0), (2, 1): ("busy.exe", 0.0)}
    sl.tick_once(100.0)
    box.run(2.0, 50.0, procs={
        (1, 1): ("idle-ish.exe", 0.1),
        (2, 1): ("busy.exe", 3.0),
        (3, 9): ("newborn.exe", 1.5),  # absent from the baseline
        (4, 1): ("medium.exe", 0.5),
    })
    sample = sl.tick_once(102.0)
    assert [(p.name, p.cores) for p in sample.top] == [
        ("busy", 3.0), ("newborn", 1.5), ("medium", 0.5),
    ]


def test_a_reused_pid_is_a_new_process_not_a_delta(box):
    box.procs = {(7, 1): ("old.exe", 500.0)}
    sl.tick_once(100.0)
    box.procs = {(7, 2): ("new.exe", 1.0)}
    sample = sl.tick_once(102.0)
    assert [(p.name, p.cores) for p in sample.top] == [("new", 0.5)]


@pytest.mark.parametrize(
    "image, cmdline, expected",
    [
        ("python.exe", ["C:\\x\\python.exe", "-m", "pytest", "-q", "tests"], "python (pytest)"),
        ("pythonw.exe", ["pythonw.exe", "-X", "utf8", "D:\\a\\b\\launcher.py", "serve"], "pythonw (launcher)"),
        ("python.exe", ["python.exe", "-c", "import x"], "python"),
        ("python.exe", [], "python"),
        ("node.exe", ["node", "/srv/app/server.js", "--port", "1"], "node (server)"),
        ("svchost.exe", ["svchost.exe", "-k", "secret"], "svchost"),
        ("chrome.exe", ["chrome.exe", "--user-data-dir=C:\\Users\\someone"], "chrome"),
        ("", [], "?"),
    ],
)
def test_display_name_never_carries_a_path_or_command_line(image, cmdline, expected):
    assert sl.display_name(image, cmdline) == expected


def test_no_sampling_without_demand(monkeypatch):
    import asyncio

    calls = []
    monkeypatch.setattr(sl, "tick_once", lambda now=None: calls.append(now))
    monkeypatch.setattr(sl, "WAKE_SECONDS", 0.0)

    async def spin():
        task = asyncio.create_task(sl.tick_forever())
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(spin())
    assert calls == []


def test_the_route_answers_from_the_cache_without_touching_psutil(webapp_client, monkeypatch, box):
    import time

    client, _app, _ = webapp_client
    # On the real clock, so the route's own "now" finds the sample current.
    _drive(box, [55.0], start=time.monotonic())

    def forbidden(*a, **k):
        raise AssertionError("the route must not sample on request")

    for name in ("cpu_times", "cpu_percent", "virtual_memory", "process_iter", "cpu_count", "Process"):
        monkeypatch.setattr(sl.psutil, name, forbidden)
    monkeypatch.setattr(sl, "_process_times", forbidden)
    body = client.get("/api/system/load").json()
    assert body["level"] == "normal"
    assert body["cpu_percent"] == 55.0


@pytest.mark.skipif(sys.platform != "win32", reason="the one-call snapshot is Windows-only")
def test_the_windows_snapshot_sees_this_process():
    import os

    procs = sl._windows_process_times()
    mine = [name for (pid, _created), (name, _secs) in procs.items() if pid == os.getpid()]
    assert mine and mine[0].lower().startswith("python")
