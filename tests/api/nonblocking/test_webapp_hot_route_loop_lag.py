"""The phone's hot polls never block the webapp's event loop for 100 ms.

Every terminal WebSocket shares the one event loop with every poll, so a
route that does synchronous work on the loop delays every keystroke echo by
as long as it runs. Twenty pollers hammer the hot routes through the real app
on the test's own loop while :class:`LoopLagMonitor` probes it; the worst lag
must stay under :data:`loop_lag.LAG_LOG_S`.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from app.webapp import loop_lag

_POLLERS = 20
_RUN_S = 1.5
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


def test_hot_polls_keep_event_loop_lag_under_bound(webapp_client):
    _, app, overrides = webapp_client
    overrides["session"].list_sessions.return_value = [_LIVE]

    async def run() -> float:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # First calls import and warm caches; the bound is about steady polling.
            for path in _HOT_ROUTES:
                assert (await client.get(path)).status_code == 200, path
            deadline = time.perf_counter() + _RUN_S

            async def poller(n: int) -> None:
                while time.perf_counter() < deadline:
                    await client.get(_HOT_ROUTES[n % len(_HOT_ROUTES)])

            monitor = loop_lag.LoopLagMonitor()

            async def probe() -> None:
                while time.perf_counter() < deadline:
                    await monitor.probe()

            await asyncio.gather(probe(), *(poller(n) for n in range(_POLLERS)))
            return monitor.worst_s

    worst = asyncio.run(run())
    assert worst < loop_lag.LAG_LOG_S, (
        f"a hot route blocked the event loop for {worst * 1000:.0f} ms "
        f"(bound {loop_lag.LAG_LOG_S * 1000:.0f} ms): synchronous work is running "
        "on the loop that carries every terminal keystroke"
    )


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
