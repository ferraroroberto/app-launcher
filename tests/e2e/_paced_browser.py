"""One paced, reused browser for every capture or design walk, harness or ad hoc.

Each TCP connection a browser opens to a local server leaves a TIME_WAIT entry
on its (ephemeral port, server port) pair for minutes, and every local web app
draws from the same host-wide ephemeral pool. A walk that opens a fresh
browser or context per screenshot starts each one with an empty HTTP cache, so
every module and stylesheet is fetched again: a one-off screenshot script once
left ~250 sockets in TIME_WAIT on one port and took the whole fleet's ports
down for minutes. `_loopback.py` keeps the *suite* off the pool; this module is
for the code that sits outside it, a script an agent writes for a capture.

`PacedWalk` is the one shape such a walk takes:

* one context and one page, reused for every capture (`walk.page`);
* more pages only by asking (`walk.extra_page()`), never past `max_parallel`
  (default 1, hard cap `HARD_MAX_PARALLEL` = 4) -- the cap is the number of
  pages open at once, because the sync API drives them from one thread;
* `walk.settle()` / `walk.visit(url)` wait for network idle, then check the
  TIME_WAIT count on the target port and sleep while it is above the budget
  (default 40), raising `PortPoolBusy` if it stays there past `pause_timeout_s`;
* `walk.report()` states the peak count and the pauses, so a walk says what it cost.

The count is lines of `netstat` in TIME_WAIT with the port at either end. A
count that cannot be read is its own state, `None` -- never a pass: the report
says NOT CONFIRMED, and `strict=True` raises `PortPoolUnknown` instead of walking
unmeasured.

    with paced_walk(browser, 8501) as walk:
        for url in urls:
            walk.visit(url)
            walk.page.screenshot(path=...)

Vendor-verbatim (manifest key `paced_browser`), stdlib + Playwright only. Never
change the machine's dynamic port range or `TcpTimedWaitDelay` to make a walk
pass: those are host-level settings. The rule and the ratchet that backs it:
docs/playwright-ui-testing.md ("One paced, reused browser for every walk").
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Optional

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright
from playwright.sync_api import Error as PlaywrightError

logger = logging.getLogger(__name__)

DEFAULT_BUDGET = 40
HARD_MAX_PARALLEL = 4
NETSTAT_TIMEOUT_S = 30
# Vendor-verbatim module: derives the flag locally (the shared `src/no_window.py` would not
# resolve in a consumer tree), the documented exception.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

#: Reads the TIME_WAIT count for a port; `None` means it could not be established.
Counter = Callable[[int], Optional[int]]  # noqa: UP007, UP045 -- Python 3.9 evaluates this alias at import, where `X | Y` raises


class PortPoolBusy(RuntimeError):
    """TIME_WAIT on the target port stayed above the budget for the whole wait."""


class PortPoolUnknown(RuntimeError):
    """`strict` walk, and the TIME_WAIT count could not be read."""


class ParallelCapExceeded(RuntimeError):
    """More pages were asked for than `max_parallel` allows."""


def _port_of(endpoint: str) -> str:
    return endpoint.rsplit(":", 1)[-1] if ":" in endpoint else ""


def count_time_wait(port: int) -> int | None:
    """Sockets in TIME_WAIT with *port* at either end, or None when `netstat` cannot be read."""
    command = ["netstat", "-n", "-p", "tcp"] if sys.platform == "win32" else ["netstat", "-an"]
    try:
        proc = subprocess.run(
            command, capture_output=True, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
            timeout=NETSTAT_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("⚠️ netstat could not be read (%s): TIME_WAIT on port %s not confirmed", exc, port)
        return None
    if proc.returncode != 0:
        logger.warning("⚠️ netstat exited %s: TIME_WAIT on port %s not confirmed", proc.returncode, port)
        return None
    # OEM code page on Windows; the fields read here are ASCII, so "replace" never changes a count.
    text = proc.stdout.decode("oem" if sys.platform == "win32" else "utf-8", "replace")
    wanted = str(port)
    total = 0
    for line in text.splitlines():
        fields = line.split()
        if "TIME_WAIT" in fields and len(fields) >= 3:
            total += wanted in (_port_of(fields[1]), _port_of(fields[2]))
    return total


class PacedWalk:
    """One reused context and page, a page cap, and a TIME_WAIT budget between captures."""

    def __init__(
        self,
        context: BrowserContext,
        port: int,
        *,
        max_parallel: int = 1,
        budget: int = DEFAULT_BUDGET,
        counter: Counter = count_time_wait,
        strict: bool = False,
        poll_interval_s: float = 2.0,
        pause_timeout_s: float = 300.0,
        idle_timeout_ms: int = 10_000,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 1 <= max_parallel <= HARD_MAX_PARALLEL:
            raise ValueError(f"max_parallel must be 1..{HARD_MAX_PARALLEL}, got {max_parallel}")
        if budget < 1:
            raise ValueError(f"budget must be at least 1, got {budget}")
        self.context = context
        self.port = port
        self.max_parallel = max_parallel
        self.budget = budget
        self._counter = counter
        self._strict = strict
        self._poll_interval_s = poll_interval_s
        self._pause_timeout_s = pause_timeout_s
        self._idle_timeout_ms = idle_timeout_ms
        self._sleep = sleep
        self._clock = clock
        self._pages: list[Page] = []
        self.peak = 0
        self.unknown_samples = 0
        self.pauses = 0
        self.paused_s = 0.0
        self.idle_timeouts = 0

    @property
    def page(self) -> Page:
        """The one reused page, created on first use."""
        if not self._pages:
            self._pages.append(self.context.new_page())
        return self._pages[0]

    def extra_page(self) -> Page:
        """A further page in the same context; refused once `max_parallel` pages are open."""
        _ = self.page  # the first page exists before the cap is counted
        if len(self._pages) >= self.max_parallel:
            raise ParallelCapExceeded(
                f"{len(self._pages)} page(s) already open and max_parallel is {self.max_parallel}; "
                "raise it deliberately (hard cap 4) or reuse walk.page"
            )
        self._pages.append(self.context.new_page())
        return self._pages[-1]

    def visit(self, url: str, **goto_kwargs: Any) -> Page:
        """Pause if the port pool is hot, load *url* on the reused page, wait for network idle."""
        self.wait_for_budget()
        self.page.goto(url, **goto_kwargs)
        self.settle()
        return self.page

    def settle(self, page: Page | None = None) -> None:
        """Wait for network idle on *page*, then hold until TIME_WAIT is back under the budget."""
        target = page or self.page
        try:
            target.wait_for_load_state("networkidle", timeout=self._idle_timeout_ms)
        except PlaywrightError:
            self.idle_timeouts += 1  # a page that polls forever never idles; the budget check still paces it
        self.wait_for_budget()

    def wait_for_budget(self) -> None:
        """Sleep while TIME_WAIT on the target port is above the budget; record the peak."""
        started = self._clock()
        paused = False
        while True:
            count = self._counter(self.port)
            if count is None:
                self.unknown_samples += 1
                if self._strict:
                    raise PortPoolUnknown(f"TIME_WAIT on port {self.port} could not be read; not walking unmeasured")
                return
            self.peak = max(self.peak, count)
            if count <= self.budget:
                break
            if not paused:
                paused = True
                self.pauses += 1
                logger.info("ℹ️ TIME_WAIT on port %s is %s (budget %s): pausing", self.port, count, self.budget)
            waited = self._clock() - started
            if waited >= self._pause_timeout_s:
                self.paused_s += waited
                raise PortPoolBusy(
                    f"TIME_WAIT on port {self.port} stayed above {self.budget} for {waited:.0f}s "
                    f"(last count {count}); another lane or a recent run holds the pool -- wait and re-run"
                )
            self._sleep(self._poll_interval_s)
        if paused:
            self.paused_s += self._clock() - started

    def report(self) -> str:
        """One line stating what the walk cost the port pool."""
        if self.unknown_samples and not self.peak:
            measured = f"TIME_WAIT on port {self.port} NOT CONFIRMED ({self.unknown_samples} unreadable samples)"
        else:
            measured = f"peak TIME_WAIT on port {self.port}: {self.peak} (budget {self.budget})"
            if self.unknown_samples:
                measured += f", {self.unknown_samples} unreadable samples"
        return (
            f"paced walk: {measured}; {self.pauses} pause(s) totalling {self.paused_s:.0f}s; "
            f"{len(self._pages)} page(s) in one context, max_parallel {self.max_parallel}"
        )


@contextmanager
def paced_walk(browser: Browser, port: int, *, context_kwargs: dict[str, Any] | None = None,
               **walk_kwargs: Any) -> Iterator[PacedWalk]:
    """One context from *browser*, a `PacedWalk` over it, the context closed and the report logged on exit."""
    context = browser.new_context(**(context_kwargs or {}))
    walk = PacedWalk(context, port, **walk_kwargs)
    try:
        yield walk
    finally:
        logger.info("✅ %s", walk.report())
        context.close()


@contextmanager
def launch_paced_walk(port: int, *, headless: bool = True, browser_name: str = "chromium",
                      launch_kwargs: dict[str, Any] | None = None, **walk_kwargs: Any) -> Iterator[PacedWalk]:
    """For a standalone script: one Playwright, one browser, one context, one `PacedWalk`; all closed on exit."""
    with sync_playwright() as playwright:
        browser = getattr(playwright, browser_name).launch(headless=headless, **(launch_kwargs or {}))
        try:
            with paced_walk(browser, port, **walk_kwargs) as walk:
                yield walk
        finally:
            browser.close()
