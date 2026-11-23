"""A stalled session, Jobs or Life OS read fills only its own pool: the Board's plan poll still answers.

Each read below hangs on an injected event (no CPU burn), and there are more of
them than its pool has threads, so that pool is full and queueing. The Board's
chief-plan poll reads its file on the Board's own pool and must answer at once,
and so must a call on the loop's default executor, sized small here so reads
left on it would fill it.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import httpx
import pytest

from app.webapp import route_pools

_HANG_S = 6.0
_BOUND_S = 1.0
_DEFAULT_WORKERS = 2


class _Stall:
    """A read that blocks until released (or :data:`_HANG_S` passes), counting entries."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.entered = 0
        self.lock = threading.Lock()
        self.release = threading.Event()

    def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
        with self.lock:
            self.entered += 1
        self.release.wait(_HANG_S)
        return self.answer


async def _until(check: Callable[[], bool], timeout: float = 3.0) -> None:
    """Wait for ``check``; on timeout, return and let the assertions say what went wrong."""
    end = time.monotonic() + timeout
    while not check() and time.monotonic() < end:
        await asyncio.sleep(0.01)


@pytest.mark.parametrize(
    ("target", "url", "pool", "answer"),
    [
        ("src.session_view.outbox.listing", "/api/claude-code/sessions/s1/outbox",
         route_pools.SESSION_FILES, {"entries": []}),
        ("src.jobs.index.search_runs", "/api/jobs/runs/search?q=x", route_pools.JOBS_FILES, []),
        ("app.webapp.routers.life_os._skills_payload", "/api/life-os/skills",
         route_pools.LIFE_OS_FILES, {"available": True, "skills": []}),
    ],
    ids=["session-outbox", "jobs-search", "life-os-skills"],
)
def test_stalled_reads_leave_the_board_plan_poll_free(webapp_client, monkeypatch, target, url, pool, answer):
    _, app, _ = webapp_client
    stall = _Stall(answer)
    monkeypatch.setattr(target, stall)
    stalls = pool._max_workers + 1

    async def run():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=_DEFAULT_WORKERS))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            reads = [
                asyncio.create_task(client.get(url))
                for _ in range(stalls)
            ]
            try:
                await _until(lambda: stall.entered >= stalls - 1)
                await asyncio.sleep(0.1)  # time for the last read to queue, not to enter
                started = time.perf_counter()
                plan = await client.get("/api/board/chief-plan")
                plan_s = time.perf_counter() - started
                started = time.perf_counter()
                await asyncio.to_thread(lambda: None)
                free_s = time.perf_counter() - started
                entered = stall.entered
            finally:
                stall.release.set()
                await asyncio.gather(*reads, return_exceptions=True)
        return plan.status_code, plan_s, free_s, entered

    status, plan_s, free_s, entered = asyncio.run(run())
    assert status == 200
    assert plan_s < _BOUND_S, (
        f"the Board's chief-plan poll took {plan_s:.2f}s behind stalled reads on {pool.pool_name}"
    )
    assert entered == stalls - 1, (
        f"{entered} of {stalls} reads ran: {pool.pool_name} is not the pool they used"
    )
    assert free_s < _BOUND_S, (
        f"a default-executor call waited {free_s:.2f}s behind stalled reads"
    )


def test_run_on_uses_the_named_pool_and_the_callers_context():
    var: contextvars.ContextVar[str] = contextvars.ContextVar("var", default="unset")

    async def run():
        var.set("request")
        return await route_pools.run_on(
            route_pools.BOARD_FILES, lambda: (threading.current_thread().name, var.get())
        )

    thread, seen = asyncio.run(run())
    assert thread.startswith("board-files")
    assert seen == "request"
