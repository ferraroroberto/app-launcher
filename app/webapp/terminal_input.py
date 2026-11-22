"""The terminal WebSocket's input side: the audit writer and keystroke timings.

A keystroke is forwarded to the session-host before anything else touches it.
Its audit line is written afterwards on :data:`_AUDIT_POOL`, a one-thread pool
of its own: the loop's default executor is shared with every route that reads
files, and on a slow machine with stalled reads it can be full for seconds, so
a keystroke that waited on it echoed seconds late. One thread keeps the log
lines in the order the frames arrived.

:class:`KeystrokeTimer` logs per-keystroke stage timings while the
``terminal_timing_log`` switch is on; with it off the proxy never builds one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Deque, Dict, Tuple

logger = logging.getLogger(__name__)
# A child of the slow-request logger so its file handler records these lines.
timing_logger = logging.getLogger("launcher.slowreq.terminal")

_AUDIT_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="terminal-audit")
# A browser `timing` frame never carries more than this many fields.
_TIMING_FIELDS = ("key_to_paint_ms", "paint_ms")


def _log_failure(future: "Future[None]") -> None:
    exc = future.exception()
    if exc is not None:
        logger.warning("⚠️ terminal audit write failed: %s", exc)


def audit_later(write: Callable[..., None], *args: Any) -> "Future[None]":
    """Queue one ``src.audit`` write behind the earlier ones; never waits for it."""
    future = _AUDIT_POOL.submit(write, *args)
    future.add_done_callback(_log_failure)
    return future


async def audit_after_pending(write: Callable[..., None], *args: Any) -> None:
    """Run one ``src.audit`` write once every queued one has landed."""
    await asyncio.wrap_future(audit_later(write, *args))


def _ms(seconds: float) -> int:
    return round(seconds * 1000)


class KeystrokeTimer:
    """Per-connection stage timings for input frames and the output that follows.

    The webapp's own stages use one clock: frame received, frame sent to the
    session-host, first output frame back. The browser's key-to-paint figure
    arrives in its own ``timing`` frame, which it sends only for a keystroke
    slower than its threshold.
    """

    def __init__(self, sid: str) -> None:
        self.sid = sid[:8]
        self._seq = 0
        self._pending: Deque[Tuple[int, float, float]] = deque()

    def forwarded(self, received: float, sent: float) -> None:
        self._seq += 1
        self._pending.append((self._seq, received, sent))

    def output(self) -> None:
        if not self._pending:
            return
        now = time.perf_counter()
        seq, received, sent = self._pending[0]
        timing_logger.info(
            "⌨️ terminal %s key #%d: webapp received→forwarded %d ms, "
            "forwarded→first output %d ms (%d frame(s) answered)",
            self.sid, seq, _ms(sent - received), _ms(now - sent), len(self._pending),
        )
        self._pending.clear()

    def browser_report(self, msg: Dict[str, Any]) -> None:
        fields = {
            name: msg[name] for name in _TIMING_FIELDS
            if isinstance(msg.get(name), (int, float))
        }
        if fields:
            timing_logger.info(
                "⌨️ terminal %s browser: %s", self.sid,
                ", ".join(f"{name} {round(value)}" for name, value in fields.items()),
            )
