"""Issue #610: audit logging inside the WS proxy hot path must not stall the
event loop.

The webapp runs a single uvicorn worker (``app/webapp/event_loop.py``), so
every live terminal's WS proxy (``app.webapp.routers.sessions._proxy_websocket``)
shares one event loop with every OTHER concurrently-open session's proxy.
``audit.session_input`` does synchronous file I/O (open/write/close) and was
called directly, unthreaded, on every single "input" frame relayed from the
phone — the highest-frequency call in the whole hot path, sustained for as
long as a terminal stays open. A slow write there (real disk contention, AV
scanning ``webapp/sessions/*.log``) would freeze every other live session's
in-flight output, a contributor to the "terminal opens blank and never
paints" class of symptom investigated in #610 — the same failure class
#639 already fixed for the session-host's ``POST /sessions`` handler, but in
the webapp process and a different call site.

This proves the fix: a slow (simulated) ``audit.session_input`` running
concurrently with another coroutine that must keep ticking on a tight
schedule must not stall that coroutine for anywhere near the write's own
duration. Exercises the real ``_proxy_websocket`` coroutine directly (not
through ``TestClient``, which runs the ASGI app on a separate portal thread
and so can't demonstrate same-loop contention).
"""

from __future__ import annotations

import asyncio
import time

from app.webapp.routers import sessions as sessions_router

_SLOW_WRITE_S = 0.3
_TICK_INTERVAL_S = 0.02
_TICK_COUNT = 20


class _FakeClientWs:
    """One "input" frame, then a disconnect — ends client_to_upstream."""

    def __init__(self) -> None:
        self._sent = False

    async def receive_text(self) -> str:
        if not self._sent:
            self._sent = True
            return '{"type": "input", "data": "hello"}'
        from starlette.websockets import WebSocketDisconnect
        raise WebSocketDisconnect(code=1000)

    async def close(self, code: int = 1000) -> None:
        pass


class _FakeUpstream:
    """Never yields a message — upstream_to_client stays pending until
    the outer ``asyncio.wait(..., FIRST_COMPLETED)`` cancels it."""

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover

    async def send(self, raw: str) -> None:
        pass


def _slow_session_input(session_id, data) -> None:
    """Stands in for audit.session_input's blocking open/write/close."""
    time.sleep(_SLOW_WRITE_S)


async def test_slow_audit_write_does_not_stall_concurrent_ws_pump(monkeypatch):
    monkeypatch.setattr(
        sessions_router.audit, "session_input", _slow_session_input
    )

    tick_gaps: list[float] = []

    async def ticker() -> None:
        last = time.perf_counter()
        for _ in range(_TICK_COUNT):
            await asyncio.sleep(_TICK_INTERVAL_S)
            now = time.perf_counter()
            tick_gaps.append(now - last)
            last = now

    async def proxy_run() -> None:
        await sessions_router._proxy_websocket(
            _FakeClientWs(), _FakeUpstream(), "sid"
        )

    # The proxy and the ticker share one event loop, exactly like a real
    # single-worker uvicorn process pumping one session's input frames
    # alongside another live session's WS proxy.
    await asyncio.gather(ticker(), proxy_run())

    max_gap = max(tick_gaps)
    assert max_gap < _SLOW_WRITE_S / 2, (
        f"a slow audit.session_input call stalled the event loop for "
        f"{max_gap:.3f}s (tick interval is {_TICK_INTERVAL_S}s) — the "
        "write is still running synchronously on the loop instead of via "
        "asyncio.to_thread"
    )
