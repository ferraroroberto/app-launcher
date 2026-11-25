"""The Apps tab's "Other ports" list, read from a background sampler.

``list_app_listeners`` walks the whole socket table and asks every owning
process for its working directory and command line. That is about a second
on a fast box and several on a slow one, and the tab used to pay it on every
poll, so on a slow box requests queued behind each other.

**One sampler, never a per-request scan.** :func:`tick_forever` (started from
the webapp lifespan) scans every :data:`TICK_SECONDS` while someone has read
the list within :data:`DEMAND_WINDOW_SECONDS` and idles otherwise.
``GET /api/ports/probe`` only calls :meth:`ListenerSampler.read`, which
answers from memory and never waits for a scan. The scan runs on its own
one-worker pool, so a slow walk stalls only this list.

**Single-flight.** A scan in progress is never joined by a second one, however
many reads or ticks arrive meanwhile.

**A change made here is not served stale.** :meth:`ListenerSampler.mark_dirty`
(a port was just killed) makes every scan that began earlier unusable, so the
next read starts a fresh scan and reads unknown (``refreshing``) until it lands.

**Unknown is its own state.** No reading yet (``warming``), a scan that has run
past :data:`SCAN_BUDGET_SECONDS` (``overrun``), a reading older than
:data:`MAX_AGE_SECONDS` (``stale``) and a scan that raised (``error``) all read
as unknown with the reason, never as an empty list: "nothing listens" and "could
not look" are different answers, and only the first may clear the rows.

Breadcrumbs go to ``webapp/slow-requests.log`` (the ``launcher.slowreq``
logger): the live webapp's own stdout is discarded by the tray.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from concurrent.futures import Executor
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from src import diagnostics
from src.diagnostics import PortOwner
from src.request_timing import TimedExecutor

breadcrumbs = logging.getLogger("launcher.slowreq")

# The tab polls every 5 s; scanning every 10 s halves the sampler's CPU and the
# list is at most one tick older than the poll that reads it.
TICK_SECONDS = 10.0
DEMAND_WINDOW_SECONDS = 60.0
# A scan running longer than this is reported, not waited for: well past a slow
# box's usual few seconds, short enough that the tab says so before the owner
# gives up on it.
SCAN_BUDGET_SECONDS = 8.0
MAX_AGE_SECONDS = 3 * TICK_SECONDS

REASON_WARMING = "warming"
REASON_REFRESHING = "refreshing"
REASON_OVERRUN = "overrun"
REASON_STALE = "stale"
REASON_ERROR = "error"

# The sampler's own thread: a slow walk then stalls only this list, never a
# request on another route's pool.
_SCAN_POOL = TimedExecutor("port-listeners-scan", max_workers=1)


@dataclass(frozen=True)
class Reading:
    """What a read returns. ``owners`` is ``None`` exactly when ``state`` is unknown."""

    state: str  # "ok" | "unknown"
    reason: Optional[str]
    detail: Optional[str]
    owners: Optional[Tuple[PortOwner, ...]]
    age_seconds: Optional[float]


@dataclass(frozen=True)
class _Snapshot:
    owners: Tuple[PortOwner, ...]
    at: float  # the clock when its scan started: the age of its oldest data


def _scan_listeners() -> List[PortOwner]:
    # Looked up per call so a test (or a future swap) of the diagnostics
    # function is honoured.
    return diagnostics.list_app_listeners()


class ListenerSampler:
    """Owns the cached listener list. Every method runs on the event loop's thread;
    only the injected ``scan`` runs on ``pool``."""

    def __init__(
        self,
        scan: Callable[[], List[PortOwner]] = _scan_listeners,
        *,
        pool: Optional[Executor] = None,
        clock: Callable[[], float] = time.monotonic,
        tick_s: float = TICK_SECONDS,
        demand_s: float = DEMAND_WINDOW_SECONDS,
        budget_s: float = SCAN_BUDGET_SECONDS,
        max_age_s: float = MAX_AGE_SECONDS,
    ) -> None:
        self._scan = scan
        self._pool = pool if pool is not None else _SCAN_POOL
        self._clock = clock
        self.tick_s = tick_s
        self.demand_s = demand_s
        self.budget_s = budget_s
        self.max_age_s = max_age_s
        self._snapshot: Optional[_Snapshot] = None
        self._inflight: Optional["asyncio.Future[List[PortOwner]]"] = None
        self._started = 0.0
        self._error: Optional[str] = None
        self._demanded_at: Optional[float] = None
        self._overrun_logged = False
        self._dirty_at = float("-inf")

    def mark_dirty(self) -> None:
        """The machine's listeners changed because of us: no earlier scan may be served."""
        self._dirty_at = self._clock()

    # ------------------------------------------------------------- scanning

    def _start(self) -> "asyncio.Future[List[PortOwner]]":
        """Begin one scan; the caller has checked none is running."""
        loop = asyncio.get_running_loop()
        self._started = self._clock()
        self._overrun_logged = False
        # An empty context: the pool's worker time must not be booked against
        # whichever request happened to kick the scan.
        future = contextvars.Context().run(loop.run_in_executor, self._pool, self._scan)
        self._inflight = future
        future.add_done_callback(self._finished)
        return future

    def _finished(self, future: "asyncio.Future[List[PortOwner]]") -> None:
        self._inflight = None
        took = self._clock() - self._started
        try:
            owners = tuple(future.result())
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 — a failed scan must not end the sampler
            first = self._error is None
            self._error = f"{type(exc).__name__}: {exc}"
            if first:
                breadcrumbs.exception("❌ port listeners scan failed; the Apps tab reads unknown")
            return
        if self._error is not None:
            breadcrumbs.info("✅ port listeners scan recovered")
        self._error = None
        self._snapshot = _Snapshot(owners, self._started)
        if took > self.budget_s:
            breadcrumbs.warning(
                "⚠️ port listeners scan took %.1fs (budget %.1fs)", took, self.budget_s
            )

    async def tick_once(self) -> None:
        """Scan when someone is watching and the reading is due, then wait for it.

        Waits on a scan another caller started too, so the tick never overlaps
        its own next scan.
        """
        now = self._clock()
        if self._inflight is None and self._watched(now) and self._due(now):
            self._start()
        future = self._inflight
        if future is not None:
            await asyncio.wait([future])

    async def tick_forever(self) -> None:
        """Keep the reading fresh while it is read; idle otherwise."""
        while True:
            await asyncio.sleep(self.tick_s)
            try:
                await self.tick_once()
            except Exception:  # noqa: BLE001 — a failed tick must not end the loop
                breadcrumbs.exception("❌ port listeners tick failed")

    def _watched(self, now: float) -> bool:
        return self._demanded_at is not None and now - self._demanded_at <= self.demand_s

    def _due(self, now: float) -> bool:
        snapshot = self._snapshot
        return snapshot is None or snapshot.at <= self._dirty_at or now - snapshot.at >= self.tick_s

    # ---------------------------------------------------------------- reads

    def read(self) -> Reading:
        """The cached list with its age. Never waits for a scan.

        The first read after an idle spell finds nothing fresh and starts one
        scan, so the list is there a poll later rather than a tick later.
        """
        now = self._clock()
        self._demanded_at = now
        snapshot = self._snapshot
        superseded = snapshot is not None and snapshot.at <= self._dirty_at
        if self._inflight is None and (snapshot is None or superseded or now - snapshot.at > self.max_age_s):
            self._start()

        if self._inflight is not None and now - self._started > self.budget_s:
            running = now - self._started
            if not self._overrun_logged:
                self._overrun_logged = True
                breadcrumbs.warning(
                    "⚠️ port listeners scan still running after %.1fs (budget %.1fs)",
                    running, self.budget_s,
                )
            return _unknown(
                REASON_OVERRUN,
                f"the port scan has run {running:.0f}s, past its {self.budget_s:.0f}s budget",
                snapshot, now,
            )
        if snapshot is None:
            if self._error is not None:
                return _unknown(REASON_ERROR, self._error, None, now)
            return _unknown(REASON_WARMING, "the first port scan is still running", None, now)
        if superseded:
            return _unknown(REASON_REFRESHING, "a port was just stopped; rescanning", None, now)
        if now - snapshot.at > self.max_age_s:
            if self._error is not None:
                return _unknown(REASON_ERROR, self._error, snapshot, now)
            return _unknown(
                REASON_STALE, f"the last port scan is {now - snapshot.at:.0f}s old", snapshot, now
            )
        return Reading("ok", None, None, snapshot.owners, round(now - snapshot.at, 1))


def _unknown(reason: str, detail: str, snapshot: Optional[_Snapshot], now: float) -> Reading:
    return Reading(
        "unknown", reason, detail, None,
        None if snapshot is None else round(now - snapshot.at, 1),
    )


# The webapp's one sampler; the lifespan ticks it and the route reads it.
SAMPLER = ListenerSampler()
