"""Bounded answers for the phone's two busiest polls (#1597).

``GET /api/claude-code/sessions`` and ``GET /api/board`` stat and read every
live session's PTY transcript (``webapp/sessions/<sid>.transcript``) on each
5 s poll. A file-system call on a transcript the session-host is still
appending to can stall below Python: on 2026-10-14 one ``os.stat`` of a 31 MB
transcript took 17.9 s in a separate probe process, in the same second the
live webapp's polls stalled (the box runs on-access antivirus scanning). Each
stalled poll held one of the loop's 20 default-executor threads, the phones
kept polling, and the pool filled. Both endpoints then answered in 29-57 s,
and every other ``asyncio.to_thread`` caller queued behind them.

So each endpoint builds its payload through a :class:`HotPoll`:

* the blocking steps run on this module's own small pool, never the loop's
  default executor, so other routes' threads can't starve these polls and
  these polls can't starve them;
* single-flight: one build per endpoint at a time. A request that arrives
  while one runs waits for the *next* build, which every caller from that
  window shares (the git-status rule, #1268), so a stuck file holds one
  build's threads, not one thread per poll;
* bounded: past :data:`DEADLINE_S` the request gets the last good payload,
  marked ``stale: true`` with its ``age_seconds``, while the build carries on
  and refreshes it for the next poll. A fresh answer is ``stale: false``.
  With no payload yet (the first poll after a restart) the request waits for
  the build, as it always did.

Breadcrumbs go to ``webapp/slow-requests.log`` at INFO: a build step over
:data:`SLOW_STEP_S` is named with its time, and the first stale answer of a
build names the steps it is still in.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from starlette.datastructures import State

slow_logger = logging.getLogger("launcher.slowreq")

# Under the phone's 2 s "this is hung" feel, well over a normal build (tens of
# milliseconds, measured on the live box).
DEADLINE_S = 1.5
SLOW_STEP_S = 2.0

# A build runs at most four steps at once (the Board's gather), one build per
# endpoint, plus a stuck build's steps that are still running.
_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="hot-poll")

Built = Tuple[float, Dict[str, Any]]


class BuildSteps:
    """One build's blocking steps: run on the hot-poll pool, timed by name."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.running: Dict[str, float] = {}
        self.reported_stale = False

    async def run(self, name: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """``fn(*args, **kwargs)`` off the loop; a run over :data:`SLOW_STEP_S` is logged."""
        loop = asyncio.get_running_loop()
        queued = time.monotonic()
        started: List[float] = []

        def call() -> Any:
            started.append(time.monotonic())
            self.running[name] = started[0]
            try:
                return fn(*args, **kwargs)
            finally:
                self.running.pop(name, None)

        try:
            return await loop.run_in_executor(_POOL, call)
        finally:
            if started:
                ran = time.monotonic() - started[0]
                if ran >= SLOW_STEP_S:
                    slow_logger.info(
                        "⏱️ slow step: %s %s took %.1fs (waited %.1fs for a thread)",
                        self.label, name, ran, started[0] - queued,
                    )

    def in_flight(self, now: float) -> str:
        return ", ".join(
            f"{name} {now - since:.1f}s" for name, since in sorted(self.running.items())
        ) or "between steps"


Build = Callable[[BuildSteps], Awaitable[Dict[str, Any]]]


class HotPoll:
    """Single-flight, deadline-bounded payload builds for one endpoint."""

    def __init__(self, label: str, deadline_s: Optional[float] = None) -> None:
        self.label = label
        self.deadline_s = DEADLINE_S if deadline_s is None else deadline_s
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running: Optional["asyncio.Task[Built]"] = None
        self._running_steps: Optional[BuildSteps] = None
        self._next: Optional["asyncio.Future[Built]"] = None
        self._next_build: Optional[Build] = None
        self._last: Optional[Built] = None

    async def get(self, build: Build) -> Dict[str, Any]:
        """The payload, fresh when a build lands within the deadline, else the last good one."""
        target = self._fresh(build)
        if self._last is None:
            return self._answer(await asyncio.shield(target), stale=False)
        try:
            return self._answer(
                await asyncio.wait_for(asyncio.shield(target), self.deadline_s), stale=False
            )
        except TimeoutError:
            pass
        steps = self._running_steps
        now = time.monotonic()
        if steps is not None and not steps.reported_stale:
            steps.reported_stale = True
            slow_logger.info(
                "⏳ %s answered from the last good payload (%.1fs old): build still "
                "running after %.1fs, in %s",
                self.label, now - self._last[0], self.deadline_s, steps.in_flight(now),
            )
        return self._answer(self._last, stale=True)

    @staticmethod
    def _answer(built: Built, *, stale: bool) -> Dict[str, Any]:
        at, payload = built
        return {**payload, "stale": stale, "age_seconds": round(time.monotonic() - at, 1)}

    def _fresh(self, build: Build) -> "asyncio.Future[Built]":
        """The build a caller arriving now waits on: one starting now, or the queued next one."""
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            # A task belongs to the loop that made it (the test client runs a
            # loop per request); the cached payload is plain data and carries over.
            self._loop, self._running, self._running_steps = loop, None, None
            self._next, self._next_build = None, None
        if self._running is None:
            return self._start(build)
        if self._next is None:
            self._next, self._next_build = loop.create_future(), build
        return self._next

    def _start(self, build: Build) -> "asyncio.Task[Built]":
        steps = BuildSteps(self.label)
        task = asyncio.ensure_future(self._build(build, steps))
        self._running, self._running_steps = task, steps
        task.add_done_callback(self._finished)
        return task

    async def _build(self, build: Build, steps: BuildSteps) -> Built:
        started = time.monotonic()
        payload = await build(steps)
        built = (time.monotonic(), payload)
        self._last = built
        if built[0] - started >= SLOW_STEP_S:
            slow_logger.info("⏱️ slow build: %s took %.1fs", self.label, built[0] - started)
        return built

    def _finished(self, task: "asyncio.Task[Built]") -> None:
        if not task.cancelled() and task.exception() is not None:
            slow_logger.warning(
                "⚠️ %s build failed: %s", self.label, task.exception().__class__.__name__
            )
        if self._running is not task:
            return
        self._running, self._running_steps = None, None
        queued, build = self._next, self._next_build
        self._next, self._next_build = None, None
        if queued is not None and build is not None and not queued.done():
            self._start(build).add_done_callback(functools.partial(_settle, queued))


def _settle(target: "asyncio.Future[Built]", source: "asyncio.Future[Built]") -> None:
    if target.done():
        return
    if source.cancelled():
        target.cancel()
    elif source.exception() is not None:
        target.set_exception(source.exception())
    else:
        target.set_result(source.result())


def hot_poll(state: State, label: str) -> HotPoll:
    """The app's :class:`HotPoll` for ``label``, made on first use, so every
    app instance (each test's included) keeps its own cache."""
    polls: Optional[Dict[str, HotPoll]] = getattr(state, "hot_polls", None)
    if polls is None:
        polls = {}
        state.hot_polls = polls
    if label not in polls:
        polls[label] = HotPoll(label)
    return polls[label]
