"""Named thread pools for the Board and session routes' blocking work.

The event loop's default executor is one shared pool: a transcript read
stalled under on-access antivirus scanning, a slow ``gh`` call or a
session-host that is not answering each hold one of its threads, and every
other route that hands work to it queues behind them. That is how the
phone's Board poll ended up waiting on an unrelated Chat pane.

So each kind of work these routes do gets its own bounded pool, and a stall
fills only the pool of its kind:

* :data:`BOARD_FILES`: the Board's own files (the chief's plan, journal,
  answer marks, proof reports, sessions state), read on every Board poll;
* :data:`SESSION_FILES`: one session's files (transcripts, outbox, resume
  lists), the reads most likely to stall;
* :data:`SESSION_HOST`: loopback HTTP calls to the session-host;
* :data:`OUTBOUND`: work that waits on another program or the network
  (a subprocess, ``gh``, the GitHub API, opening a window on the PC);
* :data:`AUDIT`: the audit log's appends, which every router makes.

The pools are :class:`TimedExecutor`, so a request's ``Server-Timing``
names the pool its time went to. A pool's threads start on first use.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
from typing import Any, Callable, TypeVar

from src.request_timing import TimedExecutor

T = TypeVar("T")

# Sizes cover a page's parallel boot reads of that kind plus a few stalled
# calls, while keeping a stuck kind from holding an unbounded number of threads.
BOARD_FILES = TimedExecutor("board-files", max_workers=6)
SESSION_FILES = TimedExecutor("session-files", max_workers=8)
SESSION_HOST = TimedExecutor("session-host-calls", max_workers=8)
OUTBOUND = TimedExecutor("route-outbound", max_workers=6)
AUDIT = TimedExecutor("audit-writes", max_workers=2)


async def run_on(pool: TimedExecutor, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """``fn(*args, **kwargs)`` on ``pool``, with the caller's context, as ``asyncio.to_thread`` does."""
    loop = asyncio.get_running_loop()
    call = functools.partial(contextvars.copy_context().run, fn, *args, **kwargs)
    return await loop.run_in_executor(pool, call)
