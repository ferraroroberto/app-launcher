"""A blocking call that holds a pool thread until released, for the pool-isolation tests."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from typing import Any

HANG_S = 6.0


class Stall:
    """A call that blocks until released (or :data:`HANG_S` passes), counting entries."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.entered = 0
        self.lock = threading.Lock()
        self.release = threading.Event()

    def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
        with self.lock:
            self.entered += 1
        self.release.wait(HANG_S)
        return self.answer


async def until(check: Callable[[], bool], timeout: float = 3.0) -> None:
    """Wait for ``check``; on timeout, return and let the assertions say what went wrong."""
    end = time.monotonic() + timeout
    while not check() and time.monotonic() < end:
        await asyncio.sleep(0.01)
