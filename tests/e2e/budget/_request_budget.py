"""Prove a page keeps its request budget: a minute visible, a minute hidden, requests counted by path.

A page's polling cost hides on a fast machine, so the browser test measures it
instead: a fake clock (`page.clock`) runs one minute with the page visible and
one minute hidden, every API request is counted by path, and the return to the
foreground is counted on its own. The fleet's rules (docs/polling-budget.md):

  * visible: at most the app's `[request_budget] visible_requests_per_minute`
    from its `.fleet.toml`;
  * hidden: zero;
  * the return: at most one catch-up read per path.

    reading = measure_request_budget(page, f"{base_url}/")
    assert_request_budget(reading, visible_budget(REPO_ROOT))

The page clock is stopped before the visible minute, so a page timer fires only
inside a counted minute's steps, and each minute ends on quiet traffic before the
count moves on: a read a visible minute started is never counted as hidden,
however slow the machine.

A fake clock belongs to the whole browser context, so the probe resumes it on every
exit, a raised one included: page timers run again afterwards. It cannot be taken
off, though: the context keeps a fake clock, ahead of the real time by what the probe
advanced, so a caller that shares contexts should give the budget test its own.

A page that sends a one-off flush as it turns hidden
(a usage log, buffered timings) names its path, allowed once:

    reading = measure_request_budget(page, url, hidden_flushes=("/api/usage/events",))

The clock advances in steps, and each step waits for the requests it started
to finish before the next, so a self-scheduling poll (next tick armed when the
read settles) is measured at its real cadence. Boot reads are not counted: the
minute starts once the page has loaded and its API traffic has been quiet for
`BOOT_QUIET_S` of real time, so an app that boots in two waves (the second
starting when the first has answered) is out of both before the count begins.

A long-lived response (an `EventSource` stream) never finishes, so a path prefix
named in `stream_paths` is counted when it opens but not waited for, and a stream
still open at the end of the hidden minute that was open when the page hid counts
as a hidden request:

    reading = measure_request_budget(page, url, stream_paths=("/api/events",))

A count the probe cannot establish raises `RequestBudgetUnknown`, never a
pass: no `.fleet.toml`, no usable `visible_requests_per_minute`, a request
that never settles, or a page whose API traffic never goes quiet. pytest reports it as an error, apart from a broken budget's
failure.

Vendor-verbatim (manifest key `request_budget_probe`); needs Playwright 1.45+
for `page.clock`.
"""

from __future__ import annotations

import logging
import time
import tomllib
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Request

logger = logging.getLogger(__name__)

MINUTE_MS = 60_000
DEFAULT_STEP_MS = 500
# How long one step may wait, in real time, for the requests it started.
SETTLE_TIMEOUT_S = 10.0
# Real-time quiet that ends a boot. WebKit announces a second wave's requests to the driver
# after the first wave's last `requestfinished`, so "nothing in flight" can be a gap between
# waves; the driver's own `networkidle` uses the same 500 ms. It is measured on the probe's
# own API requests, not the page's, because a held stream keeps `networkidle` from ever firing.
BOOT_QUIET_S = 0.5
# How long a boot may keep making API requests before the probe gives up on it.
BOOT_TIMEOUT_S = 15.0
# WebKit announces a held stream's `request` event 0.1 to 0.2 s of real time after the
# load settles, where Chromium announces it before: wait this long so a stream the page
# opens at boot is not counted as a request of the visible minute.
STREAM_BOOT_GRACE_S = 1.0
# How far ahead of the page's time the clock is stopped: the stop is refused once the page has
# passed it, so it covers the driver's round trips on a loaded box.
CLOCK_STOP_LEAD_MS = 2_000

# Makes `document.hidden` and `visibilityState` writable from the test and fires
# the same `visibilitychange` a real tab switch or screen lock would.
_HIDE_SCRIPT = """
(() => {
  let hidden = false;
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
  Object.defineProperty(document, 'visibilityState', {
    configurable: true, get: () => (hidden ? 'hidden' : 'visible'),
  });
  window.__requestBudgetSetHidden = (value) => {
    hidden = value;
    document.dispatchEvent(new Event('visibilitychange'));
  };
})();
"""


class RequestBudgetUnknown(RuntimeError):
    """The budget or the count could not be established: its own state, never a pass."""


@dataclass
class BudgetReading:
    """Requests per path in each phase of the measurement."""

    visible: Counter[str] = field(default_factory=Counter)
    hidden: Counter[str] = field(default_factory=Counter)
    catch_up: Counter[str] = field(default_factory=Counter)
    flushed: Counter[str] = field(default_factory=Counter)

    def summary(self) -> str:
        return (f"visible {sum(self.visible.values())} {dict(self.visible)}; "
                f"hidden {sum(self.hidden.values())} {dict(self.hidden)}; "
                f"catch-up {dict(self.catch_up)}; flushed on hide {dict(self.flushed)}")


class _Counter:
    """Counts API requests into the current phase and tracks the ones still out."""

    def __init__(self, page: Page, api_prefix: str, stream_paths: tuple[str, ...] = ()) -> None:
        self.page = page
        self.api_prefix = api_prefix
        self.stream_paths = stream_paths
        self.streams: set[Request] = set()
        self.phase: Counter[str] | None = None
        self.pending: set[Request] = set()
        self.last_activity = time.monotonic()
        page.on("request", self._started)
        page.on("requestfinished", self._settled)
        page.on("requestfailed", self._settled)

    def _started(self, request: Request) -> None:
        path = urlsplit(request.url).path
        if not path.startswith(self.api_prefix):
            return
        self.last_activity = time.monotonic()
        # A stream never finishes: count it, but settle() must not wait for it.
        if path.startswith(self.stream_paths):
            self.streams.add(request)
        else:
            self.pending.add(request)
        if self.phase is not None:
            self.phase[path] += 1

    def _settled(self, request: Request) -> None:
        if request in self.pending:
            self.last_activity = time.monotonic()
        self.pending.discard(request)
        self.streams.discard(request)

    def settle(self) -> None:
        """Wait (real time) until every request out has finished, then let the page's promises run."""
        deadline = time.monotonic() + SETTLE_TIMEOUT_S
        while self.pending:
            if time.monotonic() > deadline:
                raise RequestBudgetUnknown(
                    f"NOT CONFIRMED: {len(self.pending)} request(s) still out after {SETTLE_TIMEOUT_S}s"
                )
            self.page.wait_for_timeout(10)
        self.page.evaluate("() => new Promise((resolve) => queueMicrotask(resolve))")

    def wait_for_boot(self) -> None:
        """Wait (real time) until no API request has started or finished for `BOOT_QUIET_S`.

        Ends a boot and each counted minute. The window opens at the call, so a request
        event the driver has not delivered yet still lands in the phase that is ending.
        """
        self.last_activity = time.monotonic()
        deadline = time.monotonic() + BOOT_TIMEOUT_S
        while self.pending or time.monotonic() - self.last_activity < BOOT_QUIET_S:
            if time.monotonic() > deadline:
                raise RequestBudgetUnknown(
                    f"NOT CONFIRMED: the page's API traffic was not quiet for {BOOT_QUIET_S}s within {BOOT_TIMEOUT_S}s"
                )
            self.page.wait_for_timeout(10)
        self.settle()

    def stop_clock(self) -> None:
        """Stop the page clock: from here a page timer fires only inside `advance`."""
        now_ms = self.page.evaluate("Date.now()")
        try:
            self.page.clock.pause_at((now_ms + CLOCK_STOP_LEAD_MS) / 1000)
        except PlaywrightError as exc:
            raise RequestBudgetUnknown(f"NOT CONFIRMED: the page clock could not be stopped ({exc})") from exc

    def advance(self, total_ms: int, step_ms: int) -> None:
        for _ in range(total_ms // step_ms):
            self.page.clock.run_for(step_ms)
            self.settle()


@contextmanager
def _resumed_clock(page: Page) -> Iterator[None]:
    """Leave the context's clock running however the block ends.

    A clock that cannot be resumed raises, except when the block is already raising:
    then it is logged, so the measurement's own error is the one reported.
    """
    try:
        yield
    except BaseException:
        try:
            page.clock.resume()
        except PlaywrightError as exc:
            logger.warning("⚠️ the page clock was left stopped on this browser context: %s", exc)
        raise
    page.clock.resume()


def measure_request_budget(
    page: Page,
    url: str,
    *,
    api_prefix: str = "/api/",
    stream_paths: tuple[str, ...] = (),
    hidden_flushes: tuple[str, ...] = (),
    step_ms: int = DEFAULT_STEP_MS,
    minute_ms: int = MINUTE_MS,
) -> BudgetReading:
    """Load `url` on a fake clock and count its API requests: a minute visible, a minute hidden, the return.

    A request whose path starts with one of `stream_paths` is a long-lived stream: it is
    counted in the phase it opens in and never waited for. A stream that was open when
    the page hid and is still open when the hidden minute ends counts once as hidden.

    Each path in `hidden_flushes` may be requested once as the page turns hidden, before
    the page clock moves; that request is reported in `flushed`. A second one, or one a
    timer sends later, is a hidden request.
    """
    page.clock.install()
    with _resumed_clock(page):
        return _count_phases(page, url, api_prefix, stream_paths, hidden_flushes, step_ms, minute_ms)


def _count_phases(
    page: Page,
    url: str,
    api_prefix: str,
    stream_paths: tuple[str, ...],
    hidden_flushes: tuple[str, ...],
    step_ms: int,
    minute_ms: int,
) -> BudgetReading:
    page.add_init_script(_HIDE_SCRIPT)
    counter = _Counter(page, api_prefix, stream_paths)
    page.goto(url)
    page.wait_for_load_state("load")
    counter.wait_for_boot()
    counter.stop_clock()
    # Stopping jumps the clock forward, so whatever that fired is still boot traffic.
    counter.wait_for_boot()
    if stream_paths:
        page.wait_for_timeout(int(STREAM_BOOT_GRACE_S * 1000))
    reading = BudgetReading()

    counter.phase = reading.visible
    counter.advance(minute_ms, step_ms)
    counter.wait_for_boot()
    counter.phase = reading.hidden
    page.evaluate("() => window.__requestBudgetSetHidden(true)")
    open_at_hide = set(counter.streams)
    # The clock has not moved since the hide, so only the transition itself has sent these.
    counter.wait_for_boot()
    reading.flushed = reading.hidden & Counter(dict.fromkeys(hidden_flushes, 1))
    reading.hidden -= reading.flushed  # in place: counter.phase is this object
    counter.advance(minute_ms, step_ms)
    counter.wait_for_boot()
    for stream in open_at_hide & counter.streams:
        reading.hidden[urlsplit(stream.url).path] += 1
    counter.phase = reading.catch_up
    page.evaluate("() => window.__requestBudgetSetHidden(false)")
    counter.settle()
    counter.phase = None
    return reading


def request_budget_problems(reading: BudgetReading, visible_max: int) -> list[str]:
    """Each way `reading` breaks the budget; empty when it keeps it."""
    problems = []
    visible = sum(reading.visible.values())
    if visible > visible_max:
        problems.append(f"{visible} requests in a visible minute, budget {visible_max}")
    if reading.hidden:
        problems.append(f"{sum(reading.hidden.values())} requests while hidden, budget 0")
    repeated = {path: n for path, n in reading.catch_up.items() if n > 1}
    if repeated:
        problems.append(f"more than one catch-up read per path: {repeated}")
    return problems


def assert_request_budget(reading: BudgetReading, visible_max: int) -> None:
    problems = request_budget_problems(reading, visible_max)
    assert not problems, "; ".join(problems) + f" ({reading.summary()})"


def visible_budget(repo_root: Path) -> int:
    """`[request_budget] visible_requests_per_minute` from the repo's `.fleet.toml`; unknown, never a default."""
    path = repo_root / ".fleet.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise RequestBudgetUnknown(f"NOT CONFIRMED: {path} could not be read ({exc})") from exc
    table = data.get("request_budget", {})
    value = table.get("visible_requests_per_minute") if isinstance(table, dict) else None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RequestBudgetUnknown(
            "NOT CONFIRMED: no visible budget; set [request_budget] visible_requests_per_minute"
            f" (a whole number, 0 or more) in {path} (found {value!r})"
        )
    return value
