"""A fake page clock the e2e tests can jump (#1376).

The launcher's polls are plain ``setTimeout``/``setInterval`` calls, so a test
that waited out a 3 s tick (or 5 s of "nothing fires") bought nothing a clock
jump does not. Install before ``goto()`` so the page's own timers are the fake
ones; the clock keeps flowing at the real rate otherwise, so nothing else in
the page stalls.

``advance`` ends with a sentinel fetch queued behind whatever the jump fired:
it comes back only after the route handlers have seen every request ahead of
it, so a count read next is final rather than racing the network.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page

# Records the delay of every interval the page arms, so a test can wait for the
# poll it is about to jump instead of guessing when boot has armed it.
_SPY_INTERVALS = """(() => {
  const real = window.setInterval;
  window.__armed = [];
  window.setInterval = function (fn, ms, ...rest) {
    window.__armed.push(ms);
    return real.call(this, fn, ms, ...rest);
  };
})();"""


def install(page: Page) -> None:
    """Fake the page's timers. Call before the first navigation."""
    page.clock.install()
    # After the clock, so the spy wraps the fake ``setInterval``.
    page.add_init_script(_SPY_INTERVALS)
    page.route(re.compile(r".*/__clock_flush$"), lambda route: route.fulfill(status=204))


def advance(page: Page, ms: int) -> None:
    """Jump the clock ``ms`` forward, then let the requests it fired land."""
    page.clock.run_for(ms)
    page.evaluate("() => fetch('/__clock_flush').then(() => undefined)")


def wait_for_interval(page: Page, ms: int) -> None:
    """Wait until the page has armed a ``setInterval`` of ``ms``: a poll that
    boot arms only after its first fetch settles, so a jump made earlier would
    pass a poll that does not exist yet."""
    page.wait_for_function("(ms) => window.__armed.includes(ms)", arg=ms)
