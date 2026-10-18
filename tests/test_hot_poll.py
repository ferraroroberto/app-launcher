"""#1597: the sessions and Board polls answer within a bound while a build step hangs.

A file-system call on a live PTY transcript stalled for 17.9 s, and every
5 s poll that hit it held one of the loop's default-executor threads until
the pool filled: both endpoints answered in 29-57 s. Each test here hangs one
build step (a fake slow dependency) and asserts the endpoint still answers in
under 2 s, from the last good payload, marked stale.

"Under 2 s" is the constant ``hot_poll.DEADLINE_S`` (pinned below), not a stopwatch
reading: a wall-clock bound on top of the 1.5 s deadline leaves 0.5 s of margin, and
a process the OS preempts for that long went red with the code unchanged (#1707).
The tests assert the condition instead: the answer came back while the hung step
was still hung, so it cannot have waited for it.
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

_PHONE_HUNG_S = 2.0
# Far longer than any run of these tests: a hung step must still be hung when the
# answer arrives, however loaded the box. Every test releases it in a ``finally``.
_HANG_S = 60.0
_SLOW_LOGGER = "launcher.slowreq"

_LIVE = [{
    "session_id": "abc-123", "kind": "pty", "agent": "claude",
    "name": "p", "project_dir": "C:\\stub",
}]


def test_the_stale_deadline_is_under_the_phones_hung_threshold():
    assert hot_poll.DEADLINE_S < _PHONE_HUNG_S


def _hang_second_call(release: threading.Event, returned: threading.Event):
    """A step that answers at once the first time and hangs from then on.

    ``returned`` is set when the hung call finally gives up waiting.
    """
    calls = {"n": 0}

    def step(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            release.wait(_HANG_S)
            returned.set()
        return None

    return step


async def _get_twice(app, path: str, release: threading.Event, returned: threading.Event):
    """Both answers, and whether the second came back with the step still hung."""
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = (await client.get(path)).json()
            second = (await client.get(path)).json()
            still_hung = not returned.is_set()
            return first, second, still_hung
    finally:
        release.set()


def test_sessions_poll_answers_stale_within_bound_while_a_step_hangs(
    webapp_client, monkeypatch, caplog
):
    from app.webapp.routers import sessions as sessions_router

    _, app, overrides = webapp_client
    overrides["session"].list_sessions.return_value = _LIVE
    release, returned = threading.Event(), threading.Event()
    monkeypatch.setattr(
        sessions_router, "attach_provider_web_urls", _hang_second_call(release, returned)
    )

    with caplog.at_level(logging.INFO, logger=_SLOW_LOGGER):
        first, second, still_hung = asyncio.run(
            _get_twice(app, "/api/claude-code/sessions", release, returned)
        )

    assert still_hung, "the sessions poll waited for the hung step to give up"
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
    release, returned = threading.Event(), threading.Event()
    monkeypatch.setattr(
        board_router, "attach_provider_web_urls", _hang_second_call(release, returned)
    )

    with caplog.at_level(logging.INFO, logger=_SLOW_LOGGER):
        first, second, still_hung = asyncio.run(
            _get_twice(app, "/api/board", release, returned)
        )

    assert still_hung, "the board poll waited for the hung step to give up"
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
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                body = (await client.get("/api/claude-code/sessions")).json()
            hogs_still_busy = not any(hog.done() for hog in hogs)
        finally:
            release.set()
            await asyncio.gather(*hogs)
        return body, hogs_still_busy

    body, hogs_still_busy = asyncio.run(run())
    assert hogs_still_busy, "the sessions poll waited for a default-executor thread"
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
