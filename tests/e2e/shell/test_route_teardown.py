"""A route handler still running when its context closes fails nobody else's setup.

A ``route.fetch()`` handler (the ``/api/config`` overlay and the like) can be
mid-flight when its test ends. Closing the context disposes the response it is
about to read, and Playwright keeps the handler's error and rethrows it from
the *next* API call on the connection: the following test's fixture setup. The
handler here is held between its fetch and reading the body until the context
has closed, so the ordering is forced instead of left to box load.
"""

from __future__ import annotations

import time

import pytest
from playwright.sync_api import Browser, BrowserContext, Error

from tests.e2e.fixtures.routes import fulfill_edited_json
from tests.e2e.fixtures.waits import wait_until

pytestmark = pytest.mark.smoke

# Only a deadlock guard: the hold normally ends when the context has closed.
_HOLD_LIMIT_S = 10.0


class _HeldRoute:
    """A route whose ``fetch()`` returns only after ``release`` is set."""

    def __init__(self, route, release: list, pump: BrowserContext, held: list) -> None:
        self._route = route
        self._release = release
        self._pump = pump
        self._held = held

    def fetch(self):
        resp = self._route.fetch()
        self._held.append(True)
        deadline = time.monotonic() + _HOLD_LIMIT_S
        while not self._release and time.monotonic() < deadline:
            self._pump.cookies()  # yields to the test; a sleep would not
            time.sleep(0.01)
        return resp

    def __getattr__(self, name: str):
        return getattr(self._route, name)


def test_a_handler_mid_fetch_at_close_does_not_fail_the_next_api_call(
    browser: Browser, browser_context_args: dict, base_url: str
) -> None:
    context = browser.new_context(**browser_context_args)
    # The "next test": a separate context whose calls pump the connection and
    # receive a stored error.
    later = browser.new_context(**browser_context_args)
    try:
        page = context.new_page()
        page.goto(base_url, wait_until="domcontentloaded")
        held: list = []
        torn_down: list = []
        finished: list = []

        def _handler(route) -> None:
            try:
                fulfill_edited_json(_HeldRoute(route, torn_down, later, held), lambda body: None)
            finally:
                finished.append(True)

        page.route("**/api/version", _handler)
        page.evaluate("void fetch('/api/version')")
        wait_until(page, lambda: bool(held), "the handler to hold after its fetch")
        context.close()
        torn_down.append(True)

        # The handler's outcome lands while a later call pumps the connection;
        # a stored error then rethrows from the call after it.
        deadline = time.monotonic() + 5
        while not finished:
            assert time.monotonic() < deadline, "the handler never finished"
            _pump(later)
            time.sleep(0.02)
        for _ in range(3):
            _pump(later)
            time.sleep(0.1)
    finally:
        later.close()


def _pump(context: BrowserContext) -> None:
    """One cheap API call; raises whatever handler error the connection stored."""
    try:
        context.cookies()
    except Error as exc:
        raise AssertionError(f"a closed context's handler failed a later call: {exc}") from exc
