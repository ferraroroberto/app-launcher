"""The phone's hot polls never block the webapp's event loop for 100 ms.

Every terminal WebSocket shares the one event loop with every poll, so a
route that does synchronous work on the loop delays every keystroke echo by
as long as it runs. Twenty pollers hammer the hot routes through the real app
on the test's own loop while :class:`LoopLagMonitor` probes it; the worst lag
must stay under :data:`loop_lag.LAG_LOG_S`.

A loaded host (other gate lanes) also makes the loop wake late, with no route
at fault, but in bursts. A route that blocks the loop does so on every poll,
so it fails every window. The measurement therefore passes once any of
:data:`_TRIES` windows stays under the bound, and fails only when every one
of them is over.
"""

from __future__ import annotations

import asyncio
import time
from typing import Sequence

import httpx
from fastapi import FastAPI

from app.webapp import loop_lag

_POLLERS = 20
_RUN_S = 1.5
_TRIES = 3
_LIVE = {
    "session_id": "s1", "kind": "pty", "agent": "copilot", "alive": True,
    "rows": 40, "cols": 60, "project_dir": "C:/stub/proj",
}
_HOT_ROUTES = (
    "/api/claude-code/sessions",
    "/api/board",
    "/api/claude-code/sessions/s1/context",
    "/api/claude-code/sessions/s1/plan-picker",
    "/api/version",
)


async def _worst_lag(client: httpx.AsyncClient, routes: Sequence[str]) -> float:
    """The worst loop lag while :data:`_POLLERS` coroutines poll ``routes`` for one window."""
    deadline = time.perf_counter() + _RUN_S
    monitor = loop_lag.LoopLagMonitor()

    async def poller(n: int) -> None:
        while time.perf_counter() < deadline:
            await client.get(routes[n % len(routes)])

    async def probe() -> None:
        while time.perf_counter() < deadline:
            await monitor.probe()

    await asyncio.gather(probe(), *(poller(n) for n in range(_POLLERS)))
    return monitor.worst_s


def _window_lags(app, routes: Sequence[str]) -> list[float]:
    """Each window's worst lag, stopping at the first window under the bound."""

    async def run() -> list[float]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # First calls import and warm caches; the bound is about steady polling.
            for path in routes:
                assert (await client.get(path)).status_code == 200, path
            lags: list[float] = []
            while len(lags) < _TRIES:
                lags.append(await _worst_lag(client, routes))
                if lags[-1] < loop_lag.LAG_LOG_S:
                    break
            return lags

    return asyncio.run(run())


def test_hot_polls_keep_event_loop_lag_under_bound(webapp_client):
    _, app, overrides = webapp_client
    overrides["session"].list_sessions.return_value = [_LIVE]

    lags = _window_lags(app, _HOT_ROUTES)

    assert lags[-1] < loop_lag.LAG_LOG_S, (
        f"the event loop woke late in every one of {_TRIES} polling windows "
        f"({', '.join(f'{lag * 1000:.0f}' for lag in lags)} ms; bound {loop_lag.LAG_LOG_S * 1000:.0f} ms): "
        "a hot route is running synchronous work on the loop that carries every terminal "
        "keystroke, or the host stayed saturated for the whole run"
    )


def test_a_blocking_hot_route_never_passes():
    """A route that blocks the loop on every poll fails every window, so the retries hide no block."""
    app = FastAPI()

    @app.get("/blocking")
    async def blocking() -> dict:
        time.sleep(0.2)
        return {}

    lags = _window_lags(app, ("/blocking",))

    assert len(lags) == _TRIES
    assert min(lags) >= loop_lag.LAG_LOG_S


def test_monitor_sees_a_blocking_call_on_the_loop():
    """The monitor reports a deliberate on-loop block, so a pass above means something."""

    async def run() -> float:
        monitor = loop_lag.LoopLagMonitor()

        async def block() -> None:
            await asyncio.sleep(0.01)
            time.sleep(0.2)

        await asyncio.gather(monitor.probe(), block())
        return monitor.worst_s

    assert asyncio.run(run()) >= loop_lag.LAG_LOG_S
