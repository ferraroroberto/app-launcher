"""Where a request's time went: thread-pool queueing, thread work, and the budget it is held to.

A slow answer is one of two different problems. Either its work took long
(a slow git call, a stalled transcript read), or the work was fast and sat in a
full thread pool waiting for a free thread. Only the split tells them apart,
and the middleware's single elapsed number cannot give it.

Each HTTP request gets a :class:`RequestTiming`, carried in a context
variable. A :class:`TimedExecutor` stamps every callable it is given at submit
time and again when a worker thread picks it up: the gap is ``queue``, the
call itself is ``worker``. A pool submit made while a request's context is
current is charged to that request; a background tick (no request context) is
not timed. A shared single-flight build is charged to the request that started
it. Several parallel steps add up, so ``queue_ms + worker_ms`` can exceed
``total_ms``.

The budget table (:data:`BUDGET_CLASSES`) holds each request class to a soft
and a hard limit. Over soft is an "over budget" line, over hard a "budget
breach" line. ``request_budgets`` in ``webapp_config.json`` overrides a
class's limits; a class absent from it keeps the default.
"""

from __future__ import annotations

import contextvars
import logging
import os
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: The pool name for work with no pool of its own: the event loop's default
#: executor, installed by :func:`default_executor`.
DEFAULT_POOL = "default"

#: What the pool field reads when a request never used a worker thread.
NO_POOL = "none"


class RequestTiming:
    """One request's thread-pool time, safe to add to from any worker thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.queue_s = 0.0
        self.worker_s = 0.0
        self.calls = 0
        self._pools: Dict[str, int] = {}

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
            f"queue;dur={queue_s * 1000:.1f};desc=\"{pool}\", "
            f"worker;dur={worker_s * 1000:.1f};desc=\"{pool}\", "
            f"app;dur={total_s * 1000:.1f}"
        )

    def log_fields(self, total_s: float) -> str:
        with self._lock:
            queue_s, worker_s = self.queue_s, self.worker_s
        return (
            f"queue_ms={queue_s * 1000:.0f} worker_ms={worker_s * 1000:.0f} "
            f"total_ms={total_s * 1000:.0f} pool={self.pool}"
        )


current: "contextvars.ContextVar[Optional[RequestTiming]]" = contextvars.ContextVar(
    "request_timing", default=None
)


class TimedExecutor(ThreadPoolExecutor):
    """A thread pool that reports queue and worker time to the submitting request."""

    def __init__(self, pool_name: str, max_workers: int, thread_name_prefix: str = "") -> None:
        super().__init__(max_workers=max_workers, thread_name_prefix=thread_name_prefix or pool_name)
        self.pool_name = pool_name

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> "Future[Any]":
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
    """A timed stand-in for the event loop's own default executor, same size."""
    return TimedExecutor(DEFAULT_POOL, min(32, (os.cpu_count() or 1) + 4), "asyncio")


# --------------------------------------------------------------------- budgets


@dataclass(frozen=True)
class Budget:
    """Seconds a request class should stay under (``soft``) and must not pass (``hard``)."""

    soft_s: float
    hard_s: float


#: Name, method, matcher over (path, query) and default budget. The first row
#: whose matcher accepts a request is its class; order matters where two
#: could (the transcript tick before the transcript open).
BUDGET_CLASSES: Tuple[Tuple[str, str, Callable[[str, str], bool], Budget], ...] = (
    (
        "transcript-tick", "GET",
        lambda p, q: _is_transcript(p) and "after=" in q,
        Budget(0.5, 1.5),
    ),
    (
        "transcript-open", "GET",
        lambda p, q: _is_transcript(p),
        # The target names one number (1 s); soft and hard share it.
        Budget(1.0, 1.0),
    ),
    ("session-list", "GET", lambda p, q: p == "/api/claude-code/sessions", Budget(0.5, 1.5)),
    ("board", "GET", lambda p, q: p == "/api/board", Budget(0.5, 1.5)),
    (
        "send", "POST",
        lambda p, q: p.startswith("/api/claude-code/sessions/") and p.endswith("/input"),
        Budget(5.0, 10.0),
    ),
    ("git-status", "GET", lambda p, q: p == "/api/claude-code/git-status", Budget(5.0, 10.0)),
    # The shell and the jobs poll answer from memory in tens of milliseconds;
    # their hard limit is the 3 s slow-request floor.
    ("shell", "GET", lambda p, q: p == "/", Budget(0.25, 3.0)),
    ("jobs-list", "GET", lambda p, q: p == "/api/jobs", Budget(0.25, 3.0)),
)


def _is_transcript(path: str) -> bool:
    return path.startswith("/api/claude-code/sessions/") and path.endswith("/transcript")


def classify(method: str, path: str, query: str = "") -> Optional[str]:
    """The budget class of a request, or ``None`` when no budget covers it."""
    for name, verb, matches, _budget in BUDGET_CLASSES:
        if verb == method and matches(path, query):
            return name
    return None


def class_names() -> List[str]:
    return [name for name, *_ in BUDGET_CLASSES]


def default_budgets() -> Dict[str, Budget]:
    return {name: budget for name, _verb, _matches, budget in BUDGET_CLASSES}


def resolve_budgets(overrides: Optional[Mapping[str, Sequence[float]]] = None) -> Dict[str, Budget]:
    """The default table with ``overrides`` (``{class: [soft_s, hard_s]}``) laid over it.

    An override for a class that does not exist, or one that is not two
    positive numbers with ``soft <= hard``, is ignored (as is a value that is
    not an object at all), so a hand-edited config can never switch a budget off.
    """
    table = default_budgets()
    if not isinstance(overrides, Mapping):
        return table
    for name, pair in overrides.items():
        valid = _valid_pair(pair)
        if name in table and valid is not None:
            table[name] = Budget(*valid)
    return table


def _valid_pair(pair: Any) -> Optional[Tuple[float, float]]:
    try:
        soft, hard = (float(v) for v in pair)
    except (TypeError, ValueError):
        return None
    if 0 < soft <= hard:
        return soft, hard
    return None


def budget_verdict(budget: Budget, total_s: float) -> str:
    """``ok``, ``over`` (past soft) or ``breach`` (past hard)."""
    if total_s > budget.hard_s:
        return "breach"
    if total_s > budget.soft_s:
        return "over"
    return "ok"


def clean_overrides(raw: Any) -> Tuple[Dict[str, List[float]], List[str]]:
    """The usable part of a ``request_budgets`` value, and a line for each part dropped."""
    if raw is None:
        return {}, []
    if not isinstance(raw, dict):
        return {}, ["request_budgets must be an object of {class: [soft_s, hard_s]}"]
    known = class_names()
    kept: Dict[str, List[float]] = {}
    dropped: List[str] = []
    for name, pair in raw.items():
        valid = _valid_pair(pair)
        if name not in known:
            dropped.append(f"unknown class {name!r} (known: {', '.join(known)})")
        elif valid is None:
            dropped.append(f"{name!r} must be [soft_s, hard_s] with 0 < soft_s <= hard_s")
        else:
            kept[name] = list(valid)
    return kept, dropped


def load_request_budgets(raw: dict) -> Dict[str, List[float]]:
    """``request_budgets`` out of the parsed config: the usable classes, a dropped one logged, never fatal."""
    kept, dropped = clean_overrides(raw.get("request_budgets"))
    for reason in dropped:
        logger.warning(f"⚠️  request_budgets ignored: {reason}")
    return kept


# ----------------------------------------------------------------- statistics

#: In-flight peaks are kept in buckets this long, for this many buckets.
PEAK_BUCKET_S = 10.0
PEAK_BUCKETS = 90


class RequestStats:
    """What the middleware has seen since boot: in-flight peaks and budget verdicts per class."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._peaks: Deque[Tuple[int, int]] = deque(maxlen=PEAK_BUCKETS)
        self.inflight = 0
        self.started_at = clock()
        self._verdicts: Dict[str, Dict[str, int]] = {}

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

    def snapshot(self, window_s: float) -> Dict[str, Any]:
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


STATS = RequestStats()
