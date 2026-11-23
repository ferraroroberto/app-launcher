"""Named thread pools for the routes' blocking work.

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
* :data:`AUDIT`: the audit log's appends, which every router makes;
* :data:`JOBS_FILES`: the Jobs tab's reads and writes of job state (run
  records, output tails, the run index, the runtime snapshot, a run's outbox);
* :data:`JOB_SPAWNS`: handing a Job run to its detached executor, kept apart
  from the reads so a fire never queues behind a stalled history walk;
* :data:`APP_PROBES`: the Apps tab's registry reads, folder scans, listener
  scans and local health or scheme probes.
* :data:`LIFE_OS_FILES`: Life OS's own files (skill folders, conversation
  captures and indexes, the relay registry, channel profiles, archived
  backups), read by the Life OS tab and by every relay call a channel bot makes;
* :data:`SERVICE_CALLS`: HTTP calls to the sibling services (voice
  transcription, photo OCR, TTS, the LLM hub, task-os), which can each take
  seconds to minutes, kept apart so a long transcription never starves the
  short subprocess and ``gh`` calls on :data:`OUTBOUND`;
* :data:`MISC_FILES`: the smaller views' own files (fleet scope, inspiration
  sources, the context filter's mode and log, Documents, the usage log and the
  Screen time export status).

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
JOBS_FILES = TimedExecutor("jobs-files", max_workers=6)
# A fire is one short spawn hop; a few cover a burst of taps and webhooks.
JOB_SPAWNS = TimedExecutor("job-spawns", max_workers=4)
# The Apps poll probes several tunnels' health at once, each up to 3 s.
APP_PROBES = TimedExecutor("app-probes", max_workers=8)
# A Life OS boot reads skills, conversations and channels at once, and each
# relay bot polls on its own; a few more cover a stalled capture read.
LIFE_OS_FILES = TimedExecutor("life-os-files", max_workers=6)
# A recording's chunks go one at a time; a few more cover an OCR, a summary and
# a TTS probe in flight at once.
SERVICE_CALLS = TimedExecutor("service-calls", max_workers=6)
# Settings and the smaller sheets read a file or two per open, never in bulk.
MISC_FILES = TimedExecutor("misc-files", max_workers=4)


async def run_on(pool: TimedExecutor, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """``fn(*args, **kwargs)`` on ``pool``, with the caller's context, as ``asyncio.to_thread`` does."""
    loop = asyncio.get_running_loop()
    call = functools.partial(contextvars.copy_context().run, fn, *args, **kwargs)
    return await loop.run_in_executor(pool, call)
