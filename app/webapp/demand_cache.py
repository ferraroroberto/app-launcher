"""A slow read served from memory, kept fresh in the background while it is asked for.

A route whose answer costs git spawns, a session-host round trip or a directory
scan used to pay that on every poll, and on a loaded box one stalled call put a
whole poll over budget. :class:`DemandCache` builds the answer off the request
path instead:

* a background tick (:meth:`DemandCache.tick_forever`, started from the webapp
  lifespan) rebuilds every :attr:`tick_s` while a read arrived within
  :attr:`demand_s`, and idles otherwise, so an unwatched launcher does no work;
* a read finding the answer younger than :attr:`max_age_s` returns it at once;
* a read finding it older (the first after an idle spell, or a tick that fell
  behind) waits for a rebuild, so an old answer is never served as current;
* :meth:`mark_dirty` is called by every write in this process that changes the
  answer: the next read waits for a rebuild that *started after* the mark, never
  for one already running.

Every answer comes with its age, measured from the moment its build started
(the age of its oldest data). Builds are single-flight. A build over
:data:`SLOW_BUILD_S`, and a read that had to wait while the tick should have
kept the answer fresh, leave a line in ``webapp/slow-requests.log`` (the live
webapp's own stdout is discarded by the tray).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Generic, Optional, Tuple, TypeVar

T = TypeVar("T")

breadcrumbs = logging.getLogger("launcher.slowreq")

SLOW_BUILD_S = 2.0


@dataclass(frozen=True)
class _Built(Generic[T]):
    value: T
    started: float
    generation: int


class DemandCache(Generic[T]):
    def __init__(
        self,
        label: str,
        build: Callable[[], Awaitable[T]],
        *,
        tick_s: float = 5.0,
        demand_s: float = 60.0,
        max_age_s: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.label = label
        self.tick_s = tick_s
        self.demand_s = demand_s
        self.max_age_s = max_age_s
        self._build = build
        self._clock = clock
        self._built: Optional[_Built[T]] = None
        self._task: Optional["asyncio.Task[None]"] = None
        self._generation = 0
        self._demanded_at: Optional[float] = None

    def mark_dirty(self) -> None:
        """The answer changed in this process: no read may use a build begun before now."""
        self._generation += 1

    async def get(self) -> Tuple[T, float]:
        """``(answer, age in seconds)``, from memory when it is young enough."""
        now = self._clock()
        was_watched = self._watched(now)
        self._demanded_at = now
        waited = False
        while True:
            built = self._built
            if built is not None and built.generation == self._generation:
                age = self._clock() - built.started
                if age <= self.max_age_s:
                    return built.value, round(age, 1)
            if not waited and was_watched and built is not None and built.generation == self._generation:
                breadcrumbs.warning(
                    "⏱️ %s read waited for a rebuild: the answer was %.1fs old while polled",
                    self.label, self._clock() - built.started,
                )
            waited = True
            await self._rebuild()

    async def _rebuild(self) -> None:
        task = self._task
        if task is None or task.done():
            task = self._task = asyncio.ensure_future(self._run())
            # Whoever awaits sees a failure; one that left (cancelled) must not
            # leave an unretrieved exception behind.
            task.add_done_callback(lambda done: done.cancelled() or done.exception())
        await asyncio.shield(task)

    async def _run(self) -> None:
        generation = self._generation
        started = self._clock()
        value = await self._build()
        self._built = _Built(value, started, generation)
        elapsed = self._clock() - started
        if elapsed >= SLOW_BUILD_S:
            breadcrumbs.warning("⏱️ %s rebuilt in %.1fs", self.label, elapsed)

    def _watched(self, now: float) -> bool:
        return self._demanded_at is not None and now - self._demanded_at <= self.demand_s

    async def tick_once(self) -> bool:
        """Rebuild when someone is watching and the answer is due; whether it did."""
        now = self._clock()
        if not self._watched(now):
            return False
        built = self._built
        if (
            built is not None
            and built.generation == self._generation
            and now - built.started < self.tick_s
        ):
            return False
        await self._rebuild()
        return True

    async def tick_forever(self) -> None:
        """Keep the answer fresh while it is read; a failed tick leaves the older answer to age out."""
        while True:
            await asyncio.sleep(self.tick_s)
            try:
                await self.tick_once()
            except Exception:  # noqa: BLE001 — a failed tick must not end the loop
                breadcrumbs.exception("❌ %s tick failed; reads will rebuild inline", self.label)
