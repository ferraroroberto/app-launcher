"""The snapshot behind /api/version and the channel list: from memory, kept fresh while polled."""

from __future__ import annotations

import asyncio
import threading

import pytest

from app.webapp.demand_cache import DemandCache
from app.webapp.routers import life_os_channels, misc


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Source:
    """A fake slow upstream: counts builds, and can be held open."""

    def __init__(self) -> None:
        self.builds = 0
        self.gate: asyncio.Event | None = None
        self.fail = False

    async def build(self) -> dict:
        self.builds += 1
        number = self.builds
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError("upstream down")
        return {"n": number}


def make(source: Source, clock: Clock) -> DemandCache:
    return DemandCache(
        "test", source.build, tick_s=5.0, demand_s=60.0, max_age_s=15.0, clock=clock
    )


@pytest.mark.asyncio
async def test_a_young_answer_is_served_without_a_build():
    clock, source = Clock(), Source()
    cache = make(source, clock)
    assert await cache.get() == ({"n": 1}, 0.0)
    clock.now += 10
    assert await cache.get() == ({"n": 1}, 10.0)
    assert source.builds == 1


@pytest.mark.asyncio
async def test_an_old_answer_is_rebuilt_not_served_as_current():
    clock, source = Clock(), Source()
    cache = make(source, clock)
    await cache.get()
    clock.now += 16
    assert await cache.get() == ({"n": 2}, 0.0)


@pytest.mark.asyncio
async def test_the_tick_keeps_the_answer_young_so_a_read_never_waits():
    clock, source = Clock(), Source()
    cache = make(source, clock)
    await cache.get()
    for _ in range(4):
        clock.now += 5
        assert await cache.tick_once() is True
        # The upstream now hangs; the read is still answered at once.
        source.gate = asyncio.Event()
        value, age = await asyncio.wait_for(cache.get(), timeout=0.5)
        assert value == {"n": source.builds} and age == 0.0
        source.gate = None
    assert source.builds == 5


@pytest.mark.asyncio
async def test_a_read_while_the_upstream_hangs_is_served_from_the_last_tick():
    clock, source = Clock(), Source()
    cache = make(source, clock)
    await cache.get()
    clock.now += 5
    await cache.tick_once()
    source.gate = asyncio.Event()  # every later build hangs
    clock.now += 3
    value, age = await asyncio.wait_for(cache.get(), timeout=0.5)
    assert (value, age) == ({"n": 2}, 3.0)
    assert source.builds == 2


@pytest.mark.asyncio
async def test_an_unwatched_cache_does_no_work():
    clock, source = Clock(), Source()
    cache = make(source, clock)
    assert await cache.tick_once() is False  # never read
    await cache.get()
    clock.now += 61
    assert await cache.tick_once() is False  # nobody read it for a minute
    assert source.builds == 1


@pytest.mark.asyncio
async def test_a_tick_that_finds_the_answer_young_builds_nothing():
    clock, source = Clock(), Source()
    cache = make(source, clock)
    await cache.get()
    clock.now += 2
    assert await cache.tick_once() is False
    assert source.builds == 1


@pytest.mark.asyncio
async def test_concurrent_reads_share_one_build():
    clock, source = Clock(), Source()
    source.gate = asyncio.Event()
    cache = make(source, clock)
    reads = [asyncio.ensure_future(cache.get()) for _ in range(5)]
    await asyncio.sleep(0)
    source.gate.set()
    answers = await asyncio.gather(*reads)
    assert source.builds == 1
    assert {value["n"] for value, _ in answers} == {1}


@pytest.mark.asyncio
async def test_a_write_during_a_build_makes_the_next_read_wait_for_a_newer_one():
    clock, source = Clock(), Source()
    cache = make(source, clock)
    await cache.get()
    clock.now += 6
    source.gate = asyncio.Event()
    ticking = asyncio.ensure_future(cache.tick_once())
    while source.builds < 2:  # build 2 is running when the write lands
        await asyncio.sleep(0)
    cache.mark_dirty()
    read = asyncio.ensure_future(cache.get())
    await asyncio.sleep(0)
    source.gate.set()
    value, _ = await read
    await ticking
    assert value == {"n": 3}  # build 2 predates the write; build 3 was started after it


@pytest.mark.asyncio
async def test_a_write_makes_a_young_answer_stale():
    clock, source = Clock(), Source()
    cache = make(source, clock)
    await cache.get()
    cache.mark_dirty()
    assert (await cache.get())[0] == {"n": 2}


@pytest.mark.asyncio
async def test_a_failed_build_reaches_the_reader_and_the_next_read_retries():
    clock, source = Clock(), Source()
    source.fail = True
    cache = make(source, clock)
    with pytest.raises(RuntimeError):
        await cache.get()
    source.fail = False
    assert (await cache.get())[0] == {"n": 2}


@pytest.mark.asyncio
async def test_a_failed_tick_leaves_the_older_answer_to_age_out():
    clock, source = Clock(), Source()
    cache = make(source, clock)
    await cache.get()
    clock.now += 6
    source.fail = True
    with pytest.raises(RuntimeError):
        await cache.tick_once()
    value, age = await cache.get()  # the old answer is still inside max_age
    assert (value, age) == ({"n": 1}, 6.0)
    clock.now += 10
    with pytest.raises(RuntimeError):
        await cache.get()  # now too old to serve: the failure is not hidden


# --- the two routes ---------------------------------------------------------


def test_version_is_read_from_the_snapshot_not_rebuilt_per_poll(webapp_client, monkeypatch):
    client, _, overrides = webapp_client
    calls = {"head": 0}

    def head(*_a, **_k):
        calls["head"] += 1
        return "headsha1"

    monkeypatch.setattr(misc, "resolve_git_sha", head)
    first = client.get("/api/version").json()
    second = client.get("/api/version").json()
    assert calls["head"] == 1
    assert first["head_sha"] == second["head_sha"] == "headsha1"
    assert isinstance(second["age_seconds"], float)
    assert overrides["session"].identity.call_count == 1


def test_version_does_not_wait_on_a_session_host_that_hangs_after_the_snapshot(webapp_client):
    client, _, overrides = webapp_client
    client.get("/api/version")  # the first read builds the snapshot
    release = threading.Event()
    overrides["session"].identity.side_effect = lambda *_a, **_k: release.wait(5) and None
    try:
        body = client.get("/api/version").json()
    finally:
        release.set()
    assert body["session_host"]["reachable"] is False
    assert overrides["session"].identity.call_count == 1  # the hanging call was never made


def test_version_staleness_check_does_not_run_on_the_event_loop(webapp_client, monkeypatch):
    client, _, overrides = webapp_client
    overrides["session"].identity.return_value = {"git_sha": "deadbee", "started_at": "t"}
    monkeypatch.setattr(misc, "resolve_deployed_sha", lambda *a, **k: "cafebee")
    seen = {}

    def relevance(host, deployed):
        seen["thread"] = threading.current_thread()
        return True

    monkeypatch.setattr(misc, "_session_host_path_relevance", relevance)
    client.get("/api/version")
    assert seen["thread"] is not threading.main_thread()


def test_channel_list_is_read_from_the_snapshot(webapp_client, monkeypatch):
    client, _, _ = webapp_client
    scans = {"n": 0}
    real = life_os_channels.scan_skills

    def counting(path):
        scans["n"] += 1
        return real(path)

    monkeypatch.setattr(life_os_channels, "scan_skills", counting)
    first = client.get("/api/life-os/channels").json()
    second = client.get("/api/life-os/channels").json()
    assert scans["n"] <= 1
    assert "age_seconds" in second and first["profiles"] == second["profiles"]


def test_context_filter_stats_are_not_re_parsed_per_poll(webapp_client, monkeypatch):
    from app.webapp.routers import context_filter

    client, _, _ = webapp_client
    parses = {"n": 0}
    real = context_filter.read_stats

    def counting(path):
        parses["n"] += 1
        return real(path)

    monkeypatch.setattr(context_filter, "read_stats", counting)
    first = client.get("/api/context-filter").json()
    for _ in range(3):
        again = client.get("/api/context-filter").json()
    assert parses["n"] == 1
    assert again["stats"] == first["stats"] and isinstance(again["age_seconds"], float)
