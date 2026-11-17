"""One browser context per distinct context arguments per projection, reset between tests.

A fresh context per test starts with no sockets and an empty HTTP cache, so every
test pays its own connections to the app, and every one it closes parks a
loopback port in TIME_WAIT (see ``_port_pool``). Here a context outlives the test
that asked for it: each test gets a new page in it, and the reset below puts back
what a context keeps between pages.

The reset covers every piece of context state the suite relies on: the pages, the
context's routes, cookies and permission grants, and the app origin's local and
session storage and service-worker registrations. State the reset cannot undo
(init scripts, the page clock, WebSocket routes, offline mode, geolocation, extra
headers, exposed functions, context listeners) is *tainted*: a test that touches
it on a shared context has that context closed afterwards instead of reset, so a
later test never inherits it and no test has to declare that it did.

A reset that raises discards the context too, so a damaged one never reaches the
next test.
"""

from __future__ import annotations

import functools
import json
import logging
from typing import Any, Optional

from playwright.sync_api import Browser, BrowserContext, Route

# Not re-exported by ``playwright.sync_api``, but ``BrowserContext.clock`` returns it.
from playwright.sync_api._generated import Clock as _Clock

logger = logging.getLogger(__name__)

# Runs on the app's origin, after every page is closed: what outlives a page there.
_CLEAR_ORIGIN_STATE = """async () => {
  localStorage.clear();
  sessionStorage.clear();
  const registrations = await navigator.serviceWorker.getRegistrations();
  await Promise.all(registrations.map((r) => r.unregister()));
}"""

# Context-level state ``reset_context`` does not undo, by the call that sets it.
_CONTEXT_TAINTS = (
    "add_init_script", "route_web_socket", "expose_function", "expose_binding",
    "set_offline", "set_geolocation", "set_extra_http_headers", "on", "once",
)
_CLOCK_TAINTS = (
    "install", "set_fixed_time", "set_system_time", "pause_at", "fast_forward", "run_for", "resume",
)

#: The report property a test carries for the run summary: how its context was
#: obtained (``created`` / ``reused``), then what became of it (``kept`` /
#: ``tainted`` / ``reset-failed``). Reports cross xdist workers; a module counter would not.
PROPERTY = "shared_context"

_TAINTED: list[str] = []


def _taint_on_call(cls: type, name: str) -> None:
    original = getattr(cls, name)

    @functools.wraps(original)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        _TAINTED.append(f"{cls.__name__}.{name}")
        return original(*args, **kwargs)

    wrapper.__tainting__ = True  # type: ignore[attr-defined]
    setattr(cls, name, wrapper)


def install_taint_guard() -> None:
    """Make each call that sets state the reset cannot undo mark the running test's context."""
    for cls, names in ((BrowserContext, _CONTEXT_TAINTS), (_Clock, _CLOCK_TAINTS)):
        for name in names:
            if not getattr(getattr(cls, name), "__tainting__", False):
                _taint_on_call(cls, name)


def context_key(args: dict[str, Any], seeded: bool) -> str:
    """A stable identity for a set of context arguments and whether the token is seeded."""
    return json.dumps([args, seeded], sort_keys=True, default=repr)


def _blank(route: Route) -> None:
    route.fulfill(status=200, content_type="text/html", body="<!doctype html><title>reset</title>")


def reset_context(context: BrowserContext, origin: str) -> None:
    """Return *context* to what a freshly created one holds, for *origin*'s state.

    The app's origin is answered by a route, so clearing its storage opens no
    socket to the app.
    """
    for page in list(context.pages):
        page.close()
    context.unroute_all(behavior="ignoreErrors")
    context.clear_cookies()
    context.clear_permissions()
    scratch = context.new_page()
    try:
        scratch.route(f"{origin}/**", _blank)
        scratch.goto(f"{origin}/")
        scratch.evaluate(_CLEAR_ORIGIN_STATE)
    finally:
        scratch.close()


class SharedContexts:
    """The session's contexts on one browser, keyed by their context arguments."""

    def __init__(self, browser: Browser) -> None:
        self._browser = browser
        self._by_key: dict[str, BrowserContext] = {}

    def acquire(
        self, args: dict[str, Any], seed_script: Optional[str] = None
    ) -> tuple[BrowserContext, str, bool]:
        """The context for *args* (created with *seed_script* as an init script), its key, and whether it is new.

        The seed is the session-constant bearer token, added once at creation and
        never reset: it is part of the key, so a page that wants no token gets a
        context of its own.
        """
        key = context_key(args, seed_script is not None)
        context = self._by_key.get(key)
        if context is None:
            context = self._browser.new_context(**args)
            if seed_script is not None:
                context.add_init_script(seed_script)
            self._by_key[key] = context
            return context, key, True
        return context, key, False

    @staticmethod
    def begin_test() -> None:
        """Start counting taints: everything the harness did to the context so far is its own setup."""
        _TAINTED.clear()

    def release(self, context: BrowserContext, key: str, origin: str) -> str:
        """Reset *context* for the next test, or drop it when it is tainted or the reset fails.

        Returns ``kept``, ``tainted`` or ``reset-failed``.
        """
        if _TAINTED:
            logger.info("ℹ️ shared browser context discarded, the test called %s", sorted(set(_TAINTED)))
            self._discard(context, key)
            return "tainted"
        try:
            reset_context(context, origin)
        except Exception:
            logger.warning("⚠️ shared browser context reset failed; discarding it", exc_info=True)
            self._discard(context, key)
            return "reset-failed"
        return "kept"

    def _discard(self, context: BrowserContext, key: str) -> None:
        self._by_key.pop(key, None)
        try:
            context.close()
        except Exception:
            logger.warning("⚠️ discarded context did not close cleanly", exc_info=True)


def write_summary(terminalreporter: Any) -> None:
    """One line on how many contexts the run built for how many tests, read off the reports."""
    counts: dict[str, int] = {}
    for reports in terminalreporter.stats.values():
        for report in reports:
            if getattr(report, "when", "") != "teardown":
                continue
            for name, value in report.user_properties:
                if name == PROPERTY:
                    counts[value] = counts.get(value, 0) + 1
    tests = counts.get("created", 0) + counts.get("reused", 0)
    if tests:
        terminalreporter.write_line(
            f"shared contexts: {counts.get('created', 0)} created for {tests} tests "
            f"({counts.get('reused', 0)} reused, {counts.get('tainted', 0)} discarded as tainted, "
            f"{counts.get('reset-failed', 0)} discarded after a failed reset)"
        )
