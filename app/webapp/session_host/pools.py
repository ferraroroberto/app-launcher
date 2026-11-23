"""Named thread pools for the session-host's blocking work.

The event loop's default executor is one shared pool: a PTY spawn, a bulk
message's settle-and-verify wait, a graceful stop's exit poll or a large
upload's disk write each hold one of its threads, and every other call that
hands work to it queues behind them. On this process that means one slow
spawn or message could delay an unrelated session's keystrokes.

So each kind of work gets its own bounded pool, and a stall fills only the
pool of its kind:

* :data:`SPAWNS`: starting and stopping a session (a real OS spawn, a
  PowerShell ``Start-Process``, a graceful stop's exit poll);
* :data:`INPUT`: ``POST /input`` and the pending-input discard, which wait in
  real time on settle pauses, ingest checks and timed key presses;
* :data:`PTY_WRITES`: an attached terminal's keystrokes over its WebSocket,
  kept apart from :data:`INPUT` so typing never waits on a bulk message;
* :data:`UPLOADS`: an upload's disk work (directory creation, chunk writes,
  the TTL prune);
* :data:`HOUSEKEEPING`: the reaper's cleanup of ended sessions' uploads and
  its notice to the chief.

Plain :class:`~concurrent.futures.ThreadPoolExecutor` on purpose: this process
has no request-timing middleware, and keeping ``src.request_timing`` out of
its import closure keeps that module's changes from needing a ``:8446``
restart. A pool's threads start on first use.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

T = TypeVar("T")

# Sizes cover a burst of that kind (several chief lanes spawning or messaging
# at once, a phone and a PC mirror typing) plus a few stalled calls, while
# keeping a stuck kind from holding an unbounded number of threads.
SPAWNS = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sh-spawns")
INPUT = ThreadPoolExecutor(max_workers=8, thread_name_prefix="sh-input")
PTY_WRITES = ThreadPoolExecutor(max_workers=8, thread_name_prefix="sh-pty-writes")
UPLOADS = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sh-uploads")
# The reaper runs one cleanup at a time, every 30 s.
HOUSEKEEPING = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sh-housekeeping")


async def run_on(pool: ThreadPoolExecutor, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """``fn(*args, **kwargs)`` on ``pool``, with the caller's context, as ``asyncio.to_thread`` does."""
    loop = asyncio.get_running_loop()
    call = functools.partial(contextvars.copy_context().run, fn, *args, **kwargs)
    return await loop.run_in_executor(pool, call)
