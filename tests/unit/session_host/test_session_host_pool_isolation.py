"""A stalled spawn or message fills only its own session-host pool: an unrelated request still answers.

Each stalled call below hangs on an injected event (no CPU burn), and there are
more of them than its pool has threads, so that pool is full and queueing. A
request of another kind must answer at once, and so must a call on the loop's
default executor, sized small here so calls left on it would fill it.
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable
from unittest.mock import MagicMock

import httpx
import pytest

from app.webapp.session_host import pools, server
from src.session_host_input import INPUT_OK, InputOutcome

_HANG_S = 6.0
_BOUND_S = 1.0
_DEFAULT_WORKERS = 2


class _Stall:
    """A call that blocks until released (or :data:`_HANG_S` passes), counting entries."""

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


def _fake_session() -> MagicMock:
    session = MagicMock()
    session.to_api.return_value = {"session_id": "sid", "kind": "pty"}
    session.submit_input.return_value = InputOutcome(
        reason=INPUT_OK, ingested=True, submitted=True, submit_confirmed=True
    )
    return session


_CREATE = ("post", "/sessions", {"project_dir": "C:/repo"})
_INPUT = ("post", "/sessions/sid/input", {"data": "hi", "submit": True})


@pytest.mark.parametrize(
    ("stalled", "pool", "stalled_call", "other_call"),
    [
        ("submit_input", pools.INPUT, _INPUT, _CREATE),
        ("create", pools.SPAWNS, _CREATE, _INPUT),
    ],
    ids=["input-stalls-create-answers", "spawn-stalls-input-answers"],
)
def test_a_stalled_kind_leaves_other_requests_free(monkeypatch, stalled, pool, stalled_call, other_call):
    session = _fake_session()
    stall = _Stall(InputOutcome(reason=INPUT_OK, ingested=True, submitted=True, submit_confirmed=True)
                   if stalled == "submit_input" else session)
    if stalled == "submit_input":
        session.submit_input = stall
    monkeypatch.setattr(server.manager, "get", lambda sid: session)
    monkeypatch.setattr(server.manager, "create",
                        stall if stalled == "create" else (lambda *a, **k: session))
    stalls = pool._max_workers + 1

    async def run():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=_DEFAULT_WORKERS))
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            method, url, body = stalled_call
            calls = [asyncio.create_task(client.request(method, url, json=body)) for _ in range(stalls)]
            try:
                await _until(lambda: stall.entered >= stalls - 1)
                await asyncio.sleep(0.1)  # time for the last call to queue, not to enter
                method, url, body = other_call
                started = time.perf_counter()
                other = await client.request(method, url, json=body)
                other_s = time.perf_counter() - started
                started = time.perf_counter()
                await loop.run_in_executor(None, lambda: None)
                free_s = time.perf_counter() - started
                entered = stall.entered
            finally:
                stall.release.set()
                await asyncio.gather(*calls, return_exceptions=True)
        return other.status_code, other_s, free_s, entered

    status, other_s, free_s, entered = asyncio.run(run())
    assert status == 200
    assert other_s < _BOUND_S, f"an unrelated request took {other_s:.2f}s behind stalled {stalled} calls"
    assert entered == stalls - 1, f"{entered} of {stalls} {stalled} calls ran: they did not use their own pool"
    assert free_s < _BOUND_S, f"a default-executor call waited {free_s:.2f}s behind stalled {stalled} calls"


def test_a_websocket_keystroke_runs_on_the_pty_write_pool():
    seen: list[str] = []
    session = MagicMock()
    session.write.side_effect = lambda _data: seen.append(threading.current_thread().name)

    class _Socket:
        def __init__(self) -> None:
            self.frames = ['{"type": "input", "data": "x"}']

        async def receive_text(self) -> str:
            if not self.frames:
                raise asyncio.CancelledError
            return self.frames.pop()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(server._pump_from_client(_Socket(), session))
    assert seen and seen[0].startswith("sh-pty-writes")
