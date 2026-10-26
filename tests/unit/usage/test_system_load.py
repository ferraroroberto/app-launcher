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
        self.cwds: dict = {}

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
    monkeypatch.setattr(sl, "_cwd", lambda pid: fake.cwds.get(pid, ""))
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


def _burn(box: _Box, pids, per_proc: float, cmdline, image: str = "python.exe") -> dict:
    """Invented processes: ``per_proc`` cores each, all running ``cmdline``."""
    procs = {}
    for pid in pids:
        procs[(pid, 1)] = (image, per_proc)
        box.cmdlines[pid] = cmdline
    return procs


def _sample_after(box: _Box, procs: dict, cpu_percent: float):
    sl.tick_once(100.0)
    box.run(2.0, cpu_percent, procs=procs)
    return sl.tick_once(102.0)


def test_sixteen_burners_are_one_row_with_their_count_and_summed_cores(box):
    burners = _burn(box, range(100, 116), 0.7, ["python.exe", "-c", "while 1: pass", "60"])
    other = {(1, 1): ("pythonw.exe", 1.9), (2, 1): ("vmmemWSL", 1.6), (3, 1): ("MsMpEng.exe", 0.9)}
    box.cmdlines[1] = ["pythonw.exe", "D:\\x\\launcher.py"]
    sample = _sample_after(box, {**burners, **other}, 100.0)
    first = sample.top[0]
    assert (first.name, first.count, first.cores) == ("python", 16, 11.2)
    assert [p.name for p in sample.top[1:]] == ["pythonw (launcher)", "vmmemWSL"]


def test_a_lone_busy_process_is_a_count_of_one(box):
    sample = _sample_after(box, {(5, 1): ("builder.exe", 2.0)}, 20.0)
    assert [(p.name, p.count, p.cores) for p in sample.top] == [("builder", 1, 2.0)]


def test_same_image_splits_by_script_so_two_lanes_stay_two_rows(box):
    procs = _burn(box, range(10, 14), 0.5, ["python.exe", "-m", "pytest", "-q"])
    procs.update(_burn(box, [20], 1.0, ["python.exe", "D:\\a\\launcher.py"]))
    sample = _sample_after(box, procs, 50.0)
    assert [(p.name, p.count, p.cores) for p in sample.top] == [
        ("python (pytest)", 4, 2.0), ("python (launcher)", 1, 1.0),
    ]


def test_the_rows_plus_everything_else_equal_the_cpu_line(box):
    procs = _burn(box, range(100, 116), 0.25, ["python.exe", "-c", "x"])
    sample = _sample_after(box, procs, 60.0)  # 60% of 16 cores = 9.6 cores
    assert sum(p.cores for p in sample.top) == 4.0
    assert sample.other_cores == pytest.approx(9.6 - 4.0, abs=0.01)
    body = sl.read(now=102.0)
    listed = sum(p["cores"] for p in body["top"])
    assert listed + body["other_cores"] == pytest.approx(body["cpu_percent"] / 100 * body["cores"], abs=0.05)


def test_everything_else_never_goes_negative(box):
    # A process total can exceed the CPU line (different accounting): clamp.
    sample = _sample_after(box, {(5, 1): ("builder.exe", 4.0)}, 10.0)
    assert sample.other_cores == 0.0


def test_an_unknown_reading_has_no_remainder_either():
    out = sl.read(now=10.0)
    assert out["reason"] == "warming"
    assert out["top"] == [] and out["other_cores"] is None


def test_command_lines_are_read_only_for_interpreters_in_the_shown_groups(box, monkeypatch):
    asked: list = []
    monkeypatch.setattr(sl, "_cmdline", lambda pid: asked.append(pid) or [])
    procs = _burn(box, range(100, 104), 3.0, [])  # python: the top group
    procs.update({(200 + i, 1): ("chrome.exe", 2.5) for i in range(3)})  # not an interpreter
    procs[(400, 1)] = ("msedge.exe", 1.0)
    procs.update({(300 + i, 1): ("node.exe", 0.001) for i in range(50)})  # a fourth group: not shown
    _sample_after(box, procs, 100.0)
    assert sorted(asked) == [100, 101, 102, 103]


def test_command_line_reads_are_capped_per_group(box, monkeypatch):
    asked: list = []
    monkeypatch.setattr(sl, "_cmdline", lambda pid: asked.append(pid) or [])
    procs = _burn(box, range(1000, 1000 + 3 * sl.CMDLINE_READ_CAP), 0.1, [])
    sample = _sample_after(box, procs, 100.0)
    assert len(asked) == sl.CMDLINE_READ_CAP
    assert (sample.top[0].name, sample.top[0].count) == ("python", 3 * sl.CMDLINE_READ_CAP)


def test_a_group_names_the_one_repo_folder_it_runs_in(box, tmp_path):
    repo = tmp_path / "fleet-config-wt-1440"
    (repo / "tests").mkdir(parents=True)
    (repo / ".git").write_text("gitdir: elsewhere")
    procs = _burn(box, range(10, 14), 0.5, [])
    for pid in range(10, 14):
        box.cwds[pid] = str(repo / "tests")
    sample = _sample_after(box, procs, 50.0)
    assert sample.top[0].owner == "fleet-config-wt-1440"
    assert str(tmp_path) not in repr(sl.read(now=102.0))  # a folder name, never a path


def test_a_script_path_names_its_repo_before_the_cwd_does(box, tmp_path):
    repo = tmp_path / "app-launcher"
    (repo / "app").mkdir(parents=True)
    (repo / ".git").mkdir()
    procs = _burn(box, [7], 1.0, ["python.exe", str(repo / "app" / "run.py")])
    box.cwds[7] = str(tmp_path)  # not a repo
    sample = _sample_after(box, procs, 10.0)
    assert (sample.top[0].name, sample.top[0].owner) == ("python (run)", "app-launcher")


def test_a_group_spread_over_two_repos_names_neither(box, tmp_path):
    for name, pid in (("one", 10), ("two", 11)):
        (tmp_path / name / ".git").mkdir(parents=True)
        box.cwds[pid] = str(tmp_path / name)
    sample = _sample_after(box, _burn(box, [10, 11], 0.5, []), 20.0)
    assert (sample.top[0].count, sample.top[0].owner) == (2, None)


def test_a_cwd_outside_any_repo_names_nothing(box, tmp_path):
    box.cwds[10] = str(tmp_path)
    sample = _sample_after(box, _burn(box, [10], 1.0, []), 20.0)
    assert sample.top[0].owner is None


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
