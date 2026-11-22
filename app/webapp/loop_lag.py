"""Event-loop lag: how late the loop runs a callback it scheduled.

The webapp serves every terminal WebSocket and every poll from one event loop,
so any synchronous work on it delays every keystroke and echo by as long as it
runs. :class:`LoopLagMonitor` schedules a short sleep and measures how late it
wakes. :func:`tick_forever` runs it while the ``terminal_timing_log`` switch is
on and logs every stall over :data:`LAG_LOG_S`; while the switch is off it only
re-reads the switch once every :data:`IDLE_RECHECK_S`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

# A child of the slow-request logger so its file handler records these lines.
logger = logging.getLogger("launcher.slowreq.loop")

# A stall a person notices as a late echo.
LAG_LOG_S = 0.1
# Between probes: short enough to catch a stall just over LAG_LOG_S.
PROBE_INTERVAL_S = 0.05
IDLE_RECHECK_S = 5.0


class LoopLagMonitor:
    """Measures how late each ``asyncio.sleep(interval)`` wakes; keeps the worst."""

    def __init__(self, interval_s: float = PROBE_INTERVAL_S) -> None:
        self.interval_s = interval_s
        self.worst_s = 0.0

    async def probe(self) -> float:
        """One probe: the seconds past ``interval_s`` the loop took to wake."""
        started = time.perf_counter()
        await asyncio.sleep(self.interval_s)
        lag = max(0.0, time.perf_counter() - started - self.interval_s)
        self.worst_s = max(self.worst_s, lag)
        return lag


async def tick_forever(enabled: Callable[[], Any]) -> None:
    """Log each stall over :data:`LAG_LOG_S` while ``enabled()`` is true."""
    monitor = LoopLagMonitor()
    while True:
        if not enabled():
            await asyncio.sleep(IDLE_RECHECK_S)
            continue
        lag = await monitor.probe()
        if lag >= LAG_LOG_S:
            logger.info("🐢 event loop stalled %d ms", round(lag * 1000))
