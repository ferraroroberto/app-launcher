"""Deadline-based polled waits for the real-PTY tests (issue #1108).

Two tests spawn the readback child (``_pty_readback_child.py``) inside a
genuine ConPTY and then wait for it twice: first for the ``.ready`` marker it
touches once stdin is in raw mode, then for the readback file it writes once
the sentinel arrives. Each wait was a hand-picked ``for _ in range(N):
sleep(0.05)`` count — a wall-clock budget standing in for a readiness
condition, which is the species #887 and #1087 already cost this repo.

**Why the 5 s readiness budget flaked** (measured on this box 2026-09-20, five
runs per payload size, otherwise idle, clock started exactly where the test
started it — after ``spawn()`` returns):

    spawn returns in 0.02 s; the budgeted readiness wait that follows then
    takes 3.07–3.34 s, against a 5 s budget.

So the guard shipped with ~1.7 s of headroom: it tolerated a ~55 % slowdown
and no more. A full-tier gate run puts ~2450 other tests and several live
agent PTYs on the same box, which is exactly where #1108 saw it fail while
passing in isolation. The delivery wait was never close (0.03–0.08 s against
10 s) but carries the same shape, so it moves too rather than being left as
the next one to go.

The ceilings below are **backstops, not budgets** — roughly 10× the measured
idle cost, and sized so ready + delivery stays well under ``pytest.ini``'s
120 s ``pytest-timeout``, so a genuine hang fails with the named error from
here instead of inside that black box (the e2e suite's #186 reasoning). Both
are env-tunable for a slower box; neither should ever be reached on this one.

Polling starts at 5 ms and backs off to a 20 ms cap, which makes the happy
path *faster* than the fixed tick it replaces rather than merely tolerant.
Measured against a condition coming true at the real 3.1 s readiness latency,
12 reps each: the old 50 ms tick overshoots the truth by 24 ms on average
(50 ms worst), this by 12 ms (15 ms worst) — half the average and a third of
the tail. The one case the old shape wins is a condition landing exactly on a
50 ms boundary, where it overshoots ~1 ms against this helper's ~3 ms; that is
an artifact of picking round offsets, not a property of ConPTY boot times.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Callable, Optional

#: Ceiling for "the child signalled raw-mode readiness" (env: PTY_READY_CEILING_S).
READY_CEILING_S = float(os.environ.get("PTY_READY_CEILING_S", "30"))
#: Ceiling for "the child finished writing the readback file" (env: PTY_DELIVERY_CEILING_S).
DELIVERY_CEILING_S = float(os.environ.get("PTY_DELIVERY_CEILING_S", "20"))

_POLL_MIN_S = 0.005
# 20 ms, not the old fixed 50 ms tick. Measured (see module docstring): against
# a condition that comes true at 3.1 s — the real readiness cost here — a 50 ms
# cap overshoots by 24 ms on average and a 20 ms cap by ~10 ms, so this is what
# makes "the polled check returns sooner than the fixed sleep" true rather than
# merely plausible. 3.1 s at 20 ms is ~155 `Path.exists()` stats, which costs
# nothing next to a ConPTY boot.
_POLL_MAX_S = 0.02
_POLL_BACKOFF = 1.5


class PtyWaitTimeout(AssertionError):
    """A polled PTY wait hit its ceiling.

    Its own type, because the failure it reports is *not* the one these tests
    exist to catch. Byte loss at the write boundary and "the child was still
    booting" are distinct conditions and must not share a message — before
    this, an expired delivery wait fell through to the length assertion and
    reported a real-looking "lost bytes at the PTY boundary".
    """


def _fail(
    what: str, ceiling_s: float, env_var: str, detail: Optional[Callable[[], str]]
) -> PtyWaitTimeout:
    extra = ""
    if detail is not None:
        try:
            extra = detail()
        except OSError:  # the state being described vanished; the ceiling still stands
            extra = ""
    return PtyWaitTimeout(
        # ASCII only: this string is printed to the console and into the gate's
        # verify-progress.log, where a non-ASCII dash arrives as a replacement
        # character under Windows console capture.
        f"{what} within its {ceiling_s:g}s ceiling"
        + (f" ({extra})" if extra else "")
        + ". That is this wait's backstop, not a delivery defect; "
        f"check host load first, and raise {env_var} only for a genuinely slower box."
    )


def wait_until(
    predicate: Callable[[], bool],
    *,
    ceiling_s: float,
    what: str,
    env_var: str,
    detail: Optional[Callable[[], str]] = None,
) -> float:
    """Poll ``predicate`` until true and return how long that took, in seconds.

    Raises :class:`PtyWaitTimeout` naming ``what``, the ceiling it hit and the
    env var that widens it. ``detail`` is called only on failure, to describe
    the state reached (e.g. how many bytes had landed).
    """
    started = time.monotonic()
    deadline = started + ceiling_s
    interval = _POLL_MIN_S
    while True:
        if predicate():
            return time.monotonic() - started
        if time.monotonic() >= deadline:
            raise _fail(what, ceiling_s, env_var, detail)
        time.sleep(interval)
        interval = min(_POLL_MAX_S, interval * _POLL_BACKOFF)


async def await_until(
    predicate: Callable[[], bool],
    *,
    ceiling_s: float,
    what: str,
    env_var: str,
    detail: Optional[Callable[[], str]] = None,
) -> float:
    """Async twin of :func:`wait_until`, for the tests that run on an event loop."""
    started = time.monotonic()
    deadline = started + ceiling_s
    interval = _POLL_MIN_S
    while True:
        if predicate():
            return time.monotonic() - started
        if time.monotonic() >= deadline:
            raise _fail(what, ceiling_s, env_var, detail)
        await asyncio.sleep(interval)
        interval = min(_POLL_MAX_S, interval * _POLL_BACKOFF)
