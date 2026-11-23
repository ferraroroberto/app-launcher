"""Where a web request's time went: thread-pool queueing, thread work, and the budget it is held to.

A slow answer is one of two different problems. Either its work took long (a
slow git call, a stalled file read), or the work was fast and sat in a full
thread pool waiting for a free thread. Only the split tells them apart, and a
single elapsed number cannot give it.

Each HTTP request gets a :class:`RequestTiming`, carried in a context
variable. A :class:`TimedExecutor` stamps every callable it is given at submit
time and again when a worker thread picks it up: the gap is ``queue``, the
call itself is ``worker``. A submit made while a request's context is current
is charged to that request; a background tick (no request context) is not
timed. Several parallel steps add up, so ``queue_ms + worker_ms`` can exceed
``total_ms``.

:class:`RequestTimingMiddleware` (pure ASGI, so it buffers nothing) gives every
request under ``timed_prefix`` a ``Server-Timing: queue, worker, app`` header,
logs a slow request with where its time went, holds each request class the app
declares (:class:`BudgetClass`) to a soft and a hard limit, and keeps the
in-flight peak and the verdicts in a :class:`RequestStats` an endpoint can
read without starting work. The limits are the app's to expose as settings:
``budget_overrides`` is read on every request, so a change applies live.

Wiring, in the app factory::

    app.add_middleware(RequestTimingMiddleware, classes=BUDGETS, budget_overrides=settings.budgets)
    loop.set_default_executor(default_executor())   # in the lifespan
    HOT_POLL = TimedExecutor("hot-poll", 4)          # one named pool per class of work

Vendor-verbatim (manifest key ``request_timing``), stdlib only, Python 3.9+.
Rules and reasons: docs/polling-budget.md.
"""

from __future__ import annotations

import contextvars
import itertools
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, MutableMapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

#: The pool name for work with no pool of its own: the event loop's default executor.
DEFAULT_POOL = "default"
#: What the pool field reads when a request never used a worker thread.
NO_POOL = "none"

DEFAULT_SLOW_S = 3.0
# A page's boot fans out several parallel reads, so the warning floor sits well
# above that: a line here must mean "abnormal", not "page loaded".
DEFAULT_INFLIGHT_WARN = 16
# Floor between two in-flight lines, so a hammered server logs a heartbeat, not a flood.
INFLIGHT_LOG_INTERVAL_S = 30.0
#: In-flight peaks are kept in buckets this long, for this many buckets (15 minutes).
PEAK_BUCKET_S = 10.0
PEAK_BUCKETS = 90

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

logger = logging.getLogger("request_timing")


class RequestTiming:
    """One request's thread-pool time, safe to add to from any worker thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.queue_s = 0.0
        self.worker_s = 0.0
        self.calls = 0
        self._pools: dict[str, int] = {}

    def record(self, pool: str, queue_s: float, worker_s: float) -> None:
        with self._lock:
            self.queue_s += queue_s
            self.worker_s += worker_s
            self.calls += 1
            self._pools[pool] = self._pools.get(pool, 0) + 1

    @property
    def pool(self) -> str:
        """The pools this request used, ``+``-joined; ``none`` when it used no thread."""
        with self._lock:
            return "+".join(sorted(self._pools)) or NO_POOL

    def server_timing(self, total_s: float) -> str:
        """The ``Server-Timing`` value: ``queue``, ``worker`` and ``app`` (the total) in ms."""
        pool = self.pool
        with self._lock:
            queue_s, worker_s = self.queue_s, self.worker_s
        return (
            f'queue;dur={queue_s * 1000:.1f};desc="{pool}", '
            f'worker;dur={worker_s * 1000:.1f};desc="{pool}", '
            f"app;dur={total_s * 1000:.1f}"
        )

    def log_fields(self, total_s: float) -> str:
        with self._lock:
            queue_s, worker_s = self.queue_s, self.worker_s
        return (
            f"queue_ms={queue_s * 1000:.0f} worker_ms={worker_s * 1000:.0f} "
            f"total_ms={total_s * 1000:.0f} pool={self.pool}"
        )


current: contextvars.ContextVar[RequestTiming | None] = contextvars.ContextVar("request_timing", default=None)


class TimedExecutor(ThreadPoolExecutor):
    """A thread pool that reports queue and worker time to the submitting request."""

    def __init__(self, pool_name: str, max_workers: int, thread_name_prefix: str = "") -> None:
        super().__init__(max_workers=max_workers, thread_name_prefix=thread_name_prefix or pool_name)
        self.pool_name = pool_name

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        timing = current.get()
        if timing is None:
            return super().submit(fn, *args, **kwargs)
        queued = time.monotonic()

        def timed() -> Any:
            started = time.monotonic()
            try:
                return fn(*args, **kwargs)
            finally:
                timing.record(self.pool_name, started - queued, time.monotonic() - started)

        return super().submit(timed)


def default_executor() -> TimedExecutor:
    """A timed stand-in for the event loop's own default executor, the same size."""
    return TimedExecutor(DEFAULT_POOL, min(32, (os.cpu_count() or 1) + 4), "asyncio")


# --------------------------------------------------------------------- budgets


@dataclass(frozen=True)
class Budget:
    """Seconds a request class should stay under (``soft_s``) and must not pass (``hard_s``)."""

    soft_s: float
    hard_s: float


@dataclass(frozen=True)
class BudgetClass:
    """One request class: its name, HTTP method, a matcher over (path, query) and its default budget."""

    name: str
    method: str
    matches: Callable[[str, str], bool]
    budget: Budget


def classify(classes: Sequence[BudgetClass], method: str, path: str, query: str = "") -> str | None:
    """The first class whose method and matcher accept the request; ``None`` when no budget covers it."""
    for cls in classes:
        if cls.method == method and cls.matches(path, query):
            return cls.name
    return None


def _valid_pair(pair: Any) -> tuple[float, float] | None:
    try:
        soft, hard = (float(v) for v in pair)
    except (TypeError, ValueError):
        return None
    if 0 < soft <= hard:
        return soft, hard
    return None


def resolve_budgets(
    classes: Sequence[BudgetClass], overrides: Mapping[str, Sequence[float]] | None = None
) -> dict[str, Budget]:
    """The declared defaults with ``overrides`` (``{class: [soft_s, hard_s]}``) laid over them.

    An override for an unknown class, or one that is not two positive numbers
    with ``soft <= hard``, is ignored, so a hand-edited setting can never switch
    a budget off.
    """
    table = {cls.name: cls.budget for cls in classes}
    for name, pair in (overrides or {}).items():
        valid = _valid_pair(pair)
        if name in table and valid is not None:
            table[name] = Budget(*valid)
    return table


def budget_verdict(budget: Budget, total_s: float) -> str:
    """``ok``, ``over`` (past soft) or ``breach`` (past hard)."""
    if total_s > budget.hard_s:
        return "breach"
    if total_s > budget.soft_s:
        return "over"
    return "ok"


# ----------------------------------------------------------------- statistics


class RequestStats:
    """What the middleware has seen since boot: in-flight peaks and budget verdicts per class."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._peaks: deque[tuple[int, int]] = deque(maxlen=PEAK_BUCKETS)
        self.inflight = 0
        self.started_at = clock()
        self._verdicts: dict[str, dict[str, int]] = {}

    def entered(self, inflight: int) -> None:
        bucket = int(self._clock() // PEAK_BUCKET_S)
        with self._lock:
            self.inflight = inflight
            if self._peaks and self._peaks[-1][0] == bucket:
                if inflight > self._peaks[-1][1]:
                    self._peaks[-1] = (bucket, inflight)
            else:
                self._peaks.append((bucket, inflight))

    def left(self, inflight: int) -> None:
        with self._lock:
            self.inflight = inflight

    def judged(self, request_class: str, verdict: str) -> None:
        with self._lock:
            counts = self._verdicts.setdefault(request_class, {"ok": 0, "over": 0, "breach": 0})
            counts[verdict] += 1

    def max_inflight(self, window_s: float) -> int:
        """The highest in-flight count in the last ``window_s`` seconds (bucket-granular)."""
        floor = int((self._clock() - window_s) // PEAK_BUCKET_S)
        with self._lock:
            return max((peak for bucket, peak in self._peaks if bucket >= floor), default=0)

    def snapshot(self, window_s: float) -> dict[str, Any]:
        with self._lock:
            verdicts = {name: dict(counts) for name, counts in self._verdicts.items()}
            inflight = self.inflight
        return {
            "inflight": inflight,
            "max_inflight": self.max_inflight(window_s),
            "window_s": window_s,
            "uptime_s": round(self._clock() - self.started_at, 1),
            "verdicts": verdicts,
        }


# ------------------------------------------------------------------ middleware


def _with_server_timing(message: Message, total_s: float, timing: RequestTiming) -> Message:
    headers = list(message.get("headers", []))
    headers.append((b"server-timing", timing.server_timing(total_s).encode("latin-1")))
    return {**message, "headers": headers}


class RequestTimingMiddleware:
    """Times every HTTP request: ``Server-Timing``, slow and over-budget lines, in-flight stats.

    Only ``http`` scopes are timed: a WebSocket is long-lived by design and would be noise.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        classes: Sequence[BudgetClass] = (),
        budget_overrides: Callable[[], Mapping[str, Sequence[float]] | None] | None = None,
        slow_s: float = DEFAULT_SLOW_S,
        inflight_warn: int = DEFAULT_INFLIGHT_WARN,
        timed_prefix: str = "/api/",
        stats: RequestStats | None = None,
        clock: Callable[[], float] = time.monotonic,
        log: logging.Logger | None = None,
    ) -> None:
        self.app = app
        self.classes = tuple(classes)
        self._overrides = budget_overrides
        self.slow_s = slow_s
        self.inflight_warn = inflight_warn
        self.timed_prefix = timed_prefix
        self.stats = stats if stats is not None else RequestStats(clock)
        self._clock = clock
        self._log = log or logger
        # request id -> (method, path, start). One event loop, so no lock.
        self._inflight: dict[int, tuple[str, str, float]] = {}
        self._ids = itertools.count(1)
        self._last_inflight_log = float("-inf")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = str(scope.get("method", "?"))
        path = str(scope.get("path", "?"))
        rid = next(self._ids)
        start = self._clock()
        self._inflight[rid] = (method, path, start)
        self.stats.entered(len(self._inflight))
        self._maybe_log_inflight(start)

        status: dict[str, Any] = {"code": 0}
        timing = RequestTiming()
        token = current.set(timing)
        timed = path.startswith(self.timed_prefix)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message.get("status", 0)
                if timed:
                    message = _with_server_timing(message, self._clock() - start, timing)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            current.reset(token)
            self._inflight.pop(rid, None)
            self.stats.left(len(self._inflight))
            elapsed = self._clock() - start
            code = status["code"] or "?"
            if elapsed >= self.slow_s:
                self._log.warning(
                    "🐢 slow request: %s %s → %s in %.1fs (%d still in flight) [%s]",
                    method, path, code, elapsed, len(self._inflight), timing.log_fields(elapsed),
                )
            self._judge(method, path, scope, code, elapsed, timing)

    def _judge(self, method: str, path: str, scope: Scope, code: Any, elapsed: float, timing: RequestTiming) -> None:
        query = bytes(scope.get("query_string", b"")).decode("latin-1")
        name = classify(self.classes, method, path, query)
        if name is None:
            return
        budget = resolve_budgets(self.classes, self._overrides() if self._overrides else None)[name]
        verdict = budget_verdict(budget, elapsed)
        self.stats.judged(name, verdict)
        if verdict == "breach":
            self._log.warning(
                "🚨 budget breach: %s %s (%s) → %s in %.2fs, hard limit %.2fs [%s]",
                method, path, name, code, elapsed, budget.hard_s, timing.log_fields(elapsed),
            )
        elif verdict == "over":
            self._log.warning(
                "⏱️ over budget: %s %s (%s) → %s in %.2fs, soft limit %.2fs [%s]",
                method, path, name, code, elapsed, budget.soft_s, timing.log_fields(elapsed),
            )

    def _maybe_log_inflight(self, now: float) -> None:
        count = len(self._inflight)
        if count <= self.inflight_warn or now - self._last_inflight_log < INFLIGHT_LOG_INTERVAL_S:
            return
        self._last_inflight_log = now
        method, path, started = min(self._inflight.values(), key=lambda entry: entry[2])
        self._log.warning(
            "⚠️ %d requests in flight (threshold %d); oldest: %s %s, %.1fs old",
            count, self.inflight_warn, method, path, now - started,
        )
