"""A keystroke echoes through the webapp's terminal proxy within 100 ms while
the loop's default executor is saturated by polls.

On a slower machine the default executor has few threads (``min(32, cpus +
4)``), and every route that reads files does it through ``asyncio.to_thread``.
When enough of those reads stall (antivirus walking a live transcript), the
pool fills and anything else that needs a thread queues behind them. A
keystroke must never be one of those: the proxy forwards it to the
session-host before, and independently of, any thread-pool work.

Drives the real ``_proxy_websocket`` coroutine on one loop with a fake
session-host that echoes every input frame back, so the measured time is
browser-frame-in to echo-frame-out through the webapp.
"""

from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List

from starlette.websockets import WebSocketDisconnect

from app.webapp.routers import sessions as sessions_router

# A slow machine's default executor: 4 cores give min(32, 4 + 4) threads.
_DEFAULT_WORKERS = 8
_POLLERS = 20
# One stalled read per poll; long enough that a keystroke queued behind the
# pollers waits for several of them.
_POLL_READ_S = 0.3
_KEYSTROKES = 10
_KEY_GAP_S = 0.05
_ECHO_BOUND_S = 0.1


class _EchoUpstream:
    """A fake session-host: every frame sent to it comes back as PTY output."""

    def __init__(self) -> None:
        self._out: "asyncio.Queue[str]" = asyncio.Queue()
        self.received: List[str] = []

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        return await self._out.get()

    async def send(self, raw: str) -> None:
        self.received.append(raw)
        self._out.put_nowait(json.loads(raw)["data"])


class _TypingClient:
    """A browser typing one key every ``_KEY_GAP_S``; records each echo's latency."""

    def __init__(self) -> None:
        self._next = 0
        self._sent_at: dict[str, float] = {}
        self.latencies: List[float] = []
        self.done = asyncio.Event()

    async def receive_text(self) -> str:
        if self._next >= _KEYSTROKES:
            await self.done.wait()
            raise WebSocketDisconnect(code=1000)
        await asyncio.sleep(_KEY_GAP_S)
        key = f"k{self._next}"
        self._next += 1
        self._sent_at[key] = time.perf_counter()
        return json.dumps({"type": "input", "data": key})

    async def send_text(self, text: str) -> None:
        self.latencies.append(time.perf_counter() - self._sent_at[text])
        if len(self.latencies) == _KEYSTROKES:
            self.done.set()

    async def close(self, code: int = 1000) -> None:
        pass


async def _poller(stop: asyncio.Event) -> None:
    while not stop.is_set():
        await asyncio.to_thread(time.sleep, _POLL_READ_S)


async def test_keystroke_echo_stays_fast_while_default_executor_is_full(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions_router.audit, "_SESSIONS_DIR", tmp_path)
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=_DEFAULT_WORKERS)
    loop.set_default_executor(executor)
    stop = asyncio.Event()
    pollers = [asyncio.create_task(_poller(stop)) for _ in range(_POLLERS)]
    await asyncio.sleep(_POLL_READ_S / 2)  # every pool thread busy, a backlog queued

    client, upstream = _TypingClient(), _EchoUpstream()
    try:
        await asyncio.wait_for(
            sessions_router._proxy_websocket(client, upstream, "sid"), timeout=30
        )
    finally:
        stop.set()
        await asyncio.gather(*pollers)
        executor.shutdown(wait=True)

    assert [json.loads(r)["data"] for r in upstream.received] == [
        f"k{i}" for i in range(_KEYSTROKES)
    ], "keystrokes lost, duplicated or reordered on the way to the session-host"
    worst = max(client.latencies)
    assert worst < _ECHO_BOUND_S, (
        f"keystroke-to-echo took {worst * 1000:.0f} ms with the default executor "
        f"full (bound {_ECHO_BOUND_S * 1000:.0f} ms): the input frame still waits "
        "on a thread-pool slot before it is forwarded"
    )


class _ScriptedClient:
    """Sends the given frames, then waits for one echo and disconnects."""

    def __init__(self, frames: List[str]) -> None:
        self._frames = list(frames)
        self.echoed = asyncio.Event()

    async def receive_text(self) -> str:
        if self._frames:
            return self._frames.pop(0)
        await self.echoed.wait()
        raise WebSocketDisconnect(code=1000)

    async def send_text(self, text: str) -> None:
        self.echoed.set()

    async def close(self, code: int = 1000) -> None:
        pass


async def test_timing_switch_logs_per_stage_keystroke_timings(monkeypatch, tmp_path, caplog):
    from app.webapp.terminal_input import KeystrokeTimer, timing_logger

    monkeypatch.setattr(sessions_router.audit, "_SESSIONS_DIR", tmp_path)
    caplog.set_level("INFO", logger=timing_logger.name)
    frames = [
        json.dumps({"type": "timing", "key_to_paint_ms": 240, "paint_ms": 12}),
        json.dumps({"type": "input", "data": "a"}),
    ]
    client, upstream = _ScriptedClient(frames), _EchoUpstream()
    await asyncio.wait_for(
        sessions_router._proxy_websocket(client, upstream, "sid12345xyz", KeystrokeTimer("sid12345xyz")),
        timeout=5,
    )

    assert [json.loads(r)["type"] for r in upstream.received] == ["input"], (
        "a browser timing frame reached the session-host"
    )
    lines = [r.getMessage() for r in caplog.records]
    assert any("browser: key_to_paint_ms 240, paint_ms 12" in line for line in lines), lines
    assert any(
        "key #1: webapp received→forwarded" in line and "forwarded→first output" in line
        for line in lines
    ), lines


async def test_timing_switch_off_logs_nothing(monkeypatch, tmp_path, caplog):
    from app.webapp.terminal_input import timing_logger

    monkeypatch.setattr(sessions_router.audit, "_SESSIONS_DIR", tmp_path)
    caplog.set_level("INFO", logger=timing_logger.name)
    frames = [
        json.dumps({"type": "timing", "key_to_paint_ms": 240}),
        json.dumps({"type": "input", "data": "a"}),
    ]
    client, upstream = _ScriptedClient(frames), _EchoUpstream()
    await asyncio.wait_for(sessions_router._proxy_websocket(client, upstream, "sid"), timeout=5)

    assert len(upstream.received) == 1
    assert not [r for r in caplog.records if r.name == timing_logger.name]
