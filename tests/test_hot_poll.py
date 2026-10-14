"""#1597: the sessions and Board polls answer within a bound while a build step hangs.

A file-system call on a live PTY transcript stalled for 17.9 s, and every
5 s poll that hit it held one of the loop's default-executor threads until
the pool filled: both endpoints answered in 29-57 s. Each test here hangs one
build step (a fake slow dependency) and asserts the endpoint still answers in
under 2 s, from the last good payload, marked stale.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app.webapp import hot_poll

_BOUND_S = 2.0
_HANG_S = 6.0
_SLOW_LOGGER = "launcher.slowreq"

_LIVE = [{
    "session_id": "abc-123", "kind": "pty", "agent": "claude",
    "name": "p", "project_dir": "C:\\stub",
}]


def _hang_second_call(release: threading.Event):
    """A step that answers at once the first time and hangs from then on."""
    calls = {"n": 0}

    def step(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            release.wait(_HANG_S)
        return None

    return step


async def _get_twice(app, path: str, release: threading.Event):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = (await client.get(path)).json()
        started = time.perf_counter()
        second = (await client.get(path)).json()
        elapsed = time.perf_counter() - started
        release.set()
        return first, second, elapsed


def test_sessions_poll_answers_stale_within_bound_while_a_step_hangs(
    webapp_client, monkeypatch, caplog
):
    from app.webapp.routers import sessions as sessions_router

    _, app, overrides = webapp_client
    overrides["session"].list_sessions.return_value = _LIVE
    release = threading.Event()
    monkeypatch.setattr(
        sessions_router, "attach_provider_web_urls", _hang_second_call(release)
    )

    with caplog.at_level(logging.INFO, logger=_SLOW_LOGGER):
        first, second, elapsed = asyncio.run(
            _get_twice(app, "/api/claude-code/sessions", release)
        )

    assert elapsed < _BOUND_S, f"sessions poll took {elapsed:.1f}s behind a hung step"
    assert first["stale"] is False
    assert second["stale"] is True
    assert [s["session_id"] for s in second["sessions"]] == ["abc-123"]
    assert second["age_seconds"] >= 0
    assert "attach_provider_web_urls" in caplog.text, "the stale answer names the hung step"


def test_board_poll_answers_stale_within_bound_while_a_step_hangs(
    webapp_client, monkeypatch, caplog
):
    from app.webapp.routers import board as board_router

    _, app, overrides = webapp_client
    overrides["session"].list_sessions.return_value = _LIVE
    release = threading.Event()
    monkeypatch.setattr(
        board_router, "attach_provider_web_urls", _hang_second_call(release)
    )

    with caplog.at_level(logging.INFO, logger=_SLOW_LOGGER):
        first, second, elapsed = asyncio.run(_get_twice(app, "/api/board", release))

    assert elapsed < _BOUND_S, f"board poll took {elapsed:.1f}s behind a hung step"
    assert first["stale"] is False
    assert second["stale"] is True
    assert second["generated_at"] == first["generated_at"]
    assert "attach_provider_web_urls" in caplog.text


def test_sessions_poll_is_not_starved_by_a_full_default_executor(webapp_client):
    """The other half of the outage: every default-executor thread busy (here
    with sleeps; live, with polls stuck on the same file) must not hold the
    sessions poll, which builds on its own pool."""
    _, app, overrides = webapp_client
    overrides["session"].list_sessions.return_value = _LIVE
    release = threading.Event()

    async def run():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=2))
        hogs = [loop.run_in_executor(None, release.wait, _HANG_S) for _ in range(4)]
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            started = time.perf_counter()
            body = (await client.get("/api/claude-code/sessions")).json()
            elapsed = time.perf_counter() - started
        release.set()
        await asyncio.gather(*hogs)
        return body, elapsed

    body, elapsed = asyncio.run(run())
    assert elapsed < _BOUND_S, f"sessions poll waited {elapsed:.1f}s for a default-executor thread"
    assert body["stale"] is False


def test_a_caller_during_a_build_waits_for_the_next_build():
    """Never a build begun before the caller arrived (the git-status rule, #1268)."""
    poll = hot_poll.HotPoll("test", deadline_s=5.0)
    builds = {"n": 0}
    gate = asyncio.Event()

    async def build(_steps):
        builds["n"] += 1
        mine = builds["n"]
        if mine == 1:
            await gate.wait()
        return {"build": mine}

    async def run():
        first = asyncio.create_task(poll.get(build))
        await asyncio.sleep(0)
        second = asyncio.create_task(poll.get(build))
        third = asyncio.create_task(poll.get(build))
        await asyncio.sleep(0)
        gate.set()
        return await first, await second, await third

    first, second, third = asyncio.run(run())
    assert first["build"] == 1
    assert second["build"] == third["build"] == 2
    assert builds["n"] == 2


def test_a_failed_build_raises_to_its_caller_and_keeps_the_last_payload():
    poll = hot_poll.HotPoll("test", deadline_s=5.0)

    async def good(_steps):
        return {"ok": 1}

    async def bad(_steps):
        raise RuntimeError("boom")

    async def run():
        await poll.get(good)
        with pytest.raises(RuntimeError):
            await poll.get(bad)
        return await poll.get(good)

    assert asyncio.run(run())["ok"] == 1


def test_a_slow_step_is_named_in_the_breadcrumb(caplog, monkeypatch):
    monkeypatch.setattr(hot_poll, "SLOW_STEP_S", 0.05)
    poll = hot_poll.HotPoll("GET /x", deadline_s=5.0)

    async def build(steps):
        await steps.run("read_thing", time.sleep, 0.1)
        return {}

    with caplog.at_level(logging.INFO, logger=_SLOW_LOGGER):
        asyncio.run(poll.get(build))
    assert "slow step: GET /x read_thing took" in caplog.text
