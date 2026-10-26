"""Issue #610: a slow ``POST /sessions`` create must not freeze every other
in-flight WS pump on the shared event loop.

``SessionManager.create``/``create_remote`` call ``PtyProcess.spawn`` (or
``subprocess``/PowerShell ``Start-Process`` for a remote session) — a
blocking OS call. The ``POST /sessions`` handler previously called these
synchronously (no ``asyncio.to_thread``), so any concurrent create shares
the single event loop with every already-attached terminal's ``_pump_to_client``
loop: while a create blocks, no other coroutine — including delivering a
live session's queued PTY output to its WS — gets scheduled. Under a
multi-worker orchestration run (several sessions being spawned close
together), this is a real, measurable contributor to the "terminal opens
blank and never paints" class of symptom investigated in #610 (prior
mitigation: the client-side watchdog in #624).

This proves the handler no longer blocks the loop: a slow (simulated)
create running concurrently with another coroutine that must keep ticking
on a tight schedule must not stall that coroutine for anywhere near the
create's own duration.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import httpx

from app.session_host import server

_SLOW_CREATE_S = 0.3
_TICK_INTERVAL_S = 0.02
_TICK_COUNT = 20


def _slow_create(project_dir, name, flags, agent, rows=40, cols=120,
                  history_lines=None, label=""):
    """Stands in for SessionManager.create's blocking PtyProcess.spawn()."""
    time.sleep(_SLOW_CREATE_S)
    session = MagicMock()
    session.to_api.return_value = {"session_id": "sid", "kind": "pty"}
    return session


async def test_slow_create_does_not_stall_concurrent_event_loop_work(monkeypatch):
    monkeypatch.setattr(server.manager, "create", _slow_create)

    tick_gaps: list[float] = []

    async def ticker() -> None:
        last = time.perf_counter()
        for _ in range(_TICK_COUNT):
            await asyncio.sleep(_TICK_INTERVAL_S)
            now = time.perf_counter()
            tick_gaps.append(now - last)
            last = now

    async def create_request() -> None:
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.post(
                "/sessions",
                json={
                    "project_dir": r"C:\proj", "name": "proj", "flags": "",
                    "agent": "claude",
                },
            )
            assert resp.status_code == 200

    # The create request and the ticker share one event loop, exactly like
    # a real single-worker uvicorn process handling one client's terminal
    # pump alongside another client's spawn request.
    await asyncio.gather(ticker(), create_request())

    max_gap = max(tick_gaps)
    assert max_gap < _SLOW_CREATE_S / 2, (
        f"a concurrent session create stalled the event loop for {max_gap:.3f}s "
        f"(tick interval is {_TICK_INTERVAL_S}s) — the create is still running "
        "synchronously on the loop instead of via asyncio.to_thread"
    )
