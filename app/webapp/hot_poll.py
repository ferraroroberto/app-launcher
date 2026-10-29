"""Bounded answers for the phone's two busiest polls.

``GET /api/claude-code/sessions`` and ``GET /api/board`` stat and read every
live session's PTY transcript (``webapp/sessions/<sid>.transcript``) on each
5 s poll. A file-system call on a transcript the session-host is still
appending to can stall below Python for many seconds under on-access
antivirus scanning, regardless of transcript size. Each stalled poll holds
one of the loop's 20 default-executor threads; if the phones keep polling
while enough calls stall, the pool fills, both endpoints end up answering
tens of seconds late, and every other ``asyncio.to_thread`` caller queues
behind them.

So each endpoint builds its payload through a :class:`HotPoll`:

* the blocking steps run on this module's own small pool, never the loop's
  default executor, so other routes' threads can't starve these polls and
  these polls can't starve them;
* single-flight: one build per endpoint at a time. A request that arrives
  while one runs waits for the *next* build, which every caller from that
  window shares, so a stuck file holds one build's threads, not one thread
  per poll;
* bounded: past :data:`DEADLINE_S` the request gets the last good payload,
  marked ``stale: true`` with its ``age_seconds``, while the build carries on
  and refreshes it for the next poll. A fresh answer is ``stale: false``.
  With no payload yet (the first poll after a restart) the request waits for
  the build, as it always did.

Breadcrumbs go to ``webapp/slow-requests.log`` at INFO: a build step over
:data:`SLOW_STEP_S` is named with its time, and the first stale answer of a
build names the steps it is still in.

The Chat pane's per-session polls (``/context``, ``/plan-picker``,
``/transcript``, the Board drawer's ``/exchange``) read the same files, so
they go through a :class:`HotPoll` too (#1605), one per session and request
shape (:func:`session_poll`). Theirs build on a pool of their own,
:data:`SESSION_READS_POOL`: a stalled transcript then holds that pool's
threads, never the default executor every ``POST /input`` send waits on,
nor the two polls above. A fresh answer from one of them is the route's
payload as it always was; only a stale one gains ``stale: true`` and
``age_seconds``.
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

# The per-session polls' pool. A stalled build holds one thread per
# poll key (single-flight), so this leaves room for a few stuck keys while
# every other session's Chat still reads; a full pool queues here, never on
# the default executor.
SESSION_READS_POOL = ThreadPoolExecutor(max_workers=12, thread_name_prefix="session-reads")

# Per-session polls kept at once. A transcript's forward cursor is part of the
# key, so keys come and go with every tick; idle ones are dropped past this.
SESSION_POLLS_MAX = 256

Built = Tuple[float, Dict[str, Any]]


class BuildSteps:
    """One build's blocking steps: run on its poll's pool, timed by name."""

    def __init__(self, label: str, pool: Optional[ThreadPoolExecutor] = None) -> None:
        self.label = label
        self.pool = _POOL if pool is None else pool
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
            return await loop.run_in_executor(self.pool, call)
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


async def run_on_default_executor(
    _name: str, fn: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    """:meth:`BuildSteps.run`'s shape on the loop's default executor, for a
    shared reader called from a route that is not a poll (a send-time re-read)."""
    return await asyncio.to_thread(fn, *args, **kwargs)


Build = Callable[[BuildSteps], Awaitable[Dict[str, Any]]]


class HotPoll:
    """Single-flight, deadline-bounded payload builds for one endpoint.

    Keeps its own ``_running``/``_next`` bookkeeping instead of calling
    :func:`start_or_join` (#1791 audit): that function's contract is "wait
    for the next build, however long it takes", with no deadline and no
    last-good fallback. ``HotPoll.get`` needs both — a bounded wait that
    falls back to :attr:`_last` and reports which named step is still
    running — so building it on ``start_or_join`` would still need a second,
    parallel structure for the active build's :class:`BuildSteps` and
    staleness state; the one piece that *is* shared is the queued-caller
    hand-off, via :func:`_settle` (both ``HotPoll._finished`` and
    ``_start_or_join`` call it), which is where the real duplication risk
    lived.
    """

    def __init__(
        self,
        label: str,
        deadline_s: Optional[float] = None,
        *,
        pool: Optional[ThreadPoolExecutor] = None,
        mark_fresh: bool = True,
    ) -> None:
        """``mark_fresh=False`` serves a fresh payload unchanged; ``stale``
        and ``age_seconds`` then ride only an answer that is stale."""
        self.label = label
        self.deadline_s = DEADLINE_S if deadline_s is None else deadline_s
        self.pool = pool
        self.mark_fresh = mark_fresh
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
            return self._fresh_answer(await asyncio.shield(target))
        try:
            return self._fresh_answer(
                await asyncio.wait_for(asyncio.shield(target), self.deadline_s)
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

    def idle(self) -> bool:
        """No build running or queued: dropping this poll loses only its last payload."""
        return (self._running is None or self._running.done()) and self._next is None

    def _fresh_answer(self, built: Built) -> Dict[str, Any]:
        if self.mark_fresh:
            return self._answer(built, stale=False)
        return dict(built[1])

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
        steps = BuildSteps(self.label, self.pool)
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


def _settle(target: "asyncio.Future[Any]", source: "asyncio.Future[Any]") -> None:
    if target.done():
        return
    if source.cancelled():
        target.cancel()
    elif source.exception() is not None:
        target.set_exception(source.exception())
    else:
        target.set_result(source.result())


def start_or_join(
    key: Any,
    running: Dict[Any, "asyncio.Future[Any]"],
    queued: Dict[Any, "asyncio.Future[Any]"],
    build: Callable[[], Awaitable[Any]],
) -> "asyncio.Future[Any]":
    """The future a caller arriving now should await for ``key`` — the build
    already running, the one already queued behind it, or a newly started
    one — never a stale result (the git-status rule, #1268): a call that
    arrives while a build runs waits for the *next* build, shared with every
    other call from that window, so each caller sees state from at or after
    its own arrival.

    ``running``/``queued`` are the caller's own per-key maps, so each
    single-flight domain (git-status's repo scan) keeps its own
    cardinality; this function owns only the start/settle mechanics, shared
    via :func:`_settle` so every domain agrees on how a queued caller
    inherits the next build's outcome. :class:`HotPoll` is *not* one of
    those callers — see its docstring for why it keeps its own
    ``_running``/``_next`` bookkeeping instead — but shares the same
    ``_settle`` hand-off.
    """
    loop = asyncio.get_running_loop()
    current = running.get(key)
    if current is None or current.done() or current.get_loop() is not loop:
        return _start_or_join(key, running, queued, build)
    next_future = queued.get(key)
    if next_future is None or next_future.done() or next_future.get_loop() is not loop:
        next_future = loop.create_future()
        queued[key] = next_future
    return next_future


def _start_or_join(
    key: Any,
    running: Dict[Any, "asyncio.Future[Any]"],
    queued: Dict[Any, "asyncio.Future[Any]"],
    build: Callable[[], Awaitable[Any]],
) -> "asyncio.Future[Any]":
    """Start ``build()`` for ``key`` now; when it ends, start the queued one, if any."""
    task = asyncio.ensure_future(build())
    running[key] = task

    def _done(finished: "asyncio.Future[Any]") -> None:
        if running.get(key) is not finished:
            return
        del running[key]
        next_future = queued.pop(key, None)
        if next_future is not None and not next_future.done():
            follow_up = _start_or_join(key, running, queued, build)
            follow_up.add_done_callback(functools.partial(_settle, next_future))

    task.add_done_callback(_done)
    return task


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


def session_poll(state: State, label: str, sid: str, *shape: Any) -> HotPoll:
    """The app's :class:`HotPoll` for one session's ``label`` poll (#1605).

    ``shape`` is the rest of what makes two requests the same answer (a
    transcript's cursor and page size), so only identical requests share a
    build or a stale payload. Builds run on :data:`SESSION_READS_POOL`, and a
    fresh answer is the payload unchanged. Past :data:`SESSION_POLLS_MAX`
    keys, idle polls are dropped before a new one is made.
    """
    polls: Optional[Dict[Tuple[Any, ...], HotPoll]] = getattr(state, "session_polls", None)
    if polls is None:
        polls = {}
        state.session_polls = polls
    key = (label, sid, *shape)
    poll = polls.get(key)
    if poll is None:
        if len(polls) >= SESSION_POLLS_MAX:
            for idle_key in [k for k, p in polls.items() if p.idle()]:
                del polls[idle_key]
        poll = HotPoll(f"{label} {sid[:8]}", pool=SESSION_READS_POOL, mark_fresh=False)
        polls[key] = poll
    return poll
