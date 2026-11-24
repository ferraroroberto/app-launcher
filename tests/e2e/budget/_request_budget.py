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

The clock advances in steps, and each step waits for the requests it started
to finish before the next, so a self-scheduling poll (next tick armed when the
read settles) is measured at its real cadence. Boot reads are not counted: the
minute starts once the page has loaded and gone quiet.

A count the probe cannot establish raises `RequestBudgetUnknown`, never a
pass: no `.fleet.toml`, no usable `visible_requests_per_minute`, or a request
that never settles. pytest reports it as an error, apart from a broken budget's
failure.

Vendor-verbatim (manifest key `request_budget_probe`); needs Playwright 1.45+
for `page.clock`.
"""

from __future__ import annotations

import time
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, Request

MINUTE_MS = 60_000
DEFAULT_STEP_MS = 500
# How long one step may wait, in real time, for the requests it started.
SETTLE_TIMEOUT_S = 10.0

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

    def summary(self) -> str:
        return (f"visible {sum(self.visible.values())} {dict(self.visible)}; "
                f"hidden {sum(self.hidden.values())} {dict(self.hidden)}; "
                f"catch-up {dict(self.catch_up)}")


class _Counter:
    """Counts API requests into the current phase and tracks the ones still out."""

    def __init__(self, page: Page, api_prefix: str) -> None:
        self.page = page
        self.api_prefix = api_prefix
        self.phase: Counter[str] | None = None
        self.pending: set[Request] = set()
        page.on("request", self._started)
        page.on("requestfinished", self._settled)
        page.on("requestfailed", self._settled)

    def _started(self, request: Request) -> None:
        path = urlsplit(request.url).path
        if not path.startswith(self.api_prefix):
            return
        self.pending.add(request)
        if self.phase is not None:
            self.phase[path] += 1

    def _settled(self, request: Request) -> None:
        self.pending.discard(request)

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

    def advance(self, total_ms: int, step_ms: int) -> None:
        for _ in range(total_ms // step_ms):
            self.page.clock.run_for(step_ms)
            self.settle()


def measure_request_budget(
    page: Page, url: str, *, api_prefix: str = "/api/", step_ms: int = DEFAULT_STEP_MS, minute_ms: int = MINUTE_MS
) -> BudgetReading:
    """Load `url` on a fake clock and count its API requests: a minute visible, a minute hidden, the return."""
    page.clock.install()
    page.add_init_script(_HIDE_SCRIPT)
    counter = _Counter(page, api_prefix)
    page.goto(url)
    page.wait_for_load_state("load")
    counter.settle()
    reading = BudgetReading()

    counter.phase = reading.visible
    counter.advance(minute_ms, step_ms)
    counter.phase = reading.hidden
    page.evaluate("() => window.__requestBudgetSetHidden(true)")
    counter.advance(minute_ms, step_ms)
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
