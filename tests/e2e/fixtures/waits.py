"""Waiting and stable-read helpers, and the timeout budgets the suite shares."""

from __future__ import annotations

import os
import re
import time
from typing import Callable, List

from playwright.sync_api import Locator, Page, expect


def wait_until(page: Page, predicate: Callable[[], bool], what: str,
               timeout_ms: int = 5_000, interval_ms: int = 20) -> None:
    """Wait until ``predicate()`` holds, or fail naming ``what`` (#1376).

    The event-driven replacement for ``page.wait_for_timeout(N)`` followed by
    an assertion on something a route handler records: it returns the moment
    the thing has happened instead of after a fixed guess, and a thing that
    never happens is a named failure instead of whatever the later assertion
    says. Route handlers run while Playwright waits, which is why the poll
    waits through ``page`` rather than sleeping.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout_ms} ms waiting for {what}")
        page.wait_for_timeout(interval_ms)


def flush_requests(page: Page) -> None:
    """Return once every request the page has already issued has reached its
    route handler.

    For the negative half of a check ("dismissing the confirm must not stop
    the chief"): the guess it replaces is a sleep long enough for a wrongly
    fired request to show up. A sentinel fetch queued behind whatever the page
    sent comes back only after the handlers have seen everything ahead of it,
    so a count read next is final.
    """
    if not getattr(page, "_flush_route_installed", False):
        page.route(re.compile(r".*/__flush$"), lambda route: route.fulfill(status=204))
        page._flush_route_installed = True  # type: ignore[attr-defined]
    page.evaluate("() => fetch('/__flush').then(() => undefined)")


def watch_static_failures(page: Page) -> List[str]:
    """Record every ``/static/`` load of ``page`` that fails or answers non-2xx (#1609).

    A stylesheet that never arrives does not stop the page: the scripts still
    run, the DOM still builds, and every CSS-dependent read quietly returns the
    UA default for the page's whole life (``white-space: normal`` on a title
    the vendored ``action-row.css`` would set to ``nowrap``). Auto-retrying an
    ``expect`` against that never recovers, so a test asserting on computed
    style needs to know whether the sheet loaded at all. Attach before
    ``goto()``; the returned list fills in as loads fail.
    """
    failures: List[str] = []
    page.on("requestfailed", lambda req: failures.append(
        f"{req.url.split('/static/')[-1]} ({req.failure})"
    ) if "/static/" in req.url else None)
    page.on("response", lambda resp: failures.append(
        f"{resp.url.split('/static/')[-1]} (HTTP {resp.status})"
    ) if "/static/" in resp.url and resp.status >= 400 else None)
    return failures


def stylesheet_report(page: Page) -> str:
    """One line per stylesheet naming whether its rules reached the page (#1609)."""
    sheets = page.evaluate(
        "() => [...document.styleSheets].map(s => {"
        " let n; try { n = s.cssRules.length; } catch (e) { n = 'blocked'; }"
        " return (s.href || 'inline').split('/static/').pop() + '=' + n; })"
    )
    return ", ".join(sheets)


def boot_retrying_static_failure(page: Page, boot: Callable[[], None]) -> None:
    """Run ``boot`` (a navigation plus whatever it waits for), once more if a
    ``/static/`` load failed during it, and name the files if it fails twice (#1610).

    A script or stylesheet that fails to load under gate load leaves the page
    half-built for its whole life: a failed module in the import graph means
    ``main.js`` never runs and the server-filled parts keep their markup
    defaults (the header's ``data-level="unknown"`` / ``–``), a failed sheet
    leaves UA-default styles (#1609). No wait recovers either, so the first
    failure gets one fresh boot. The watcher is installed here, ahead of
    ``boot``'s ``goto()``, so the caller attaches none. The module graph and the
    head's sheets have all settled by ``domcontentloaded``, so the list is
    final once ``boot`` returns and the page's own requests have been flushed.
    """
    failures = watch_static_failures(page)
    for attempt in (1, 2):
        if attempt == 2:
            # Leaving the broken page cancels whatever its import graph still
            # had in flight, and those ``ERR_ABORTED`` reports are ours, not
            # the new boot's: let them land before the list is cleared.
            page.goto("about:blank")
        failures.clear()
        boot()
        flush_requests(page)
        if not failures:
            return
        assert attempt == 1, f"static assets failed to load twice: {failures}"


def stable_read(read: Callable[[], object], attempts: int = 50,
                interval_s: float = 0.1) -> object:
    """Retry a raw DOM measurement past a mid-render stale element handle.

    Playwright's ``locator.evaluate()`` / ``bounding_box()`` resolve the
    selector to an element handle and *then* read from it. Any surface that
    rebuilds its DOM on a timer can invalidate that handle in between, and the
    read silently returns an artifact rather than raising: WebKit yields ``''``
    from ``getComputedStyle`` (shorthand *and* longhand) and ``None`` from
    ``bounding_box()``; ``scrollWidth``/``clientWidth`` both read ``0``.

    The Board is exactly such a surface — ``renderBoard()`` builds a fresh
    ``<li>`` for every card on every render (``placeChildren()`` keeps only an
    open drawer's), and it renders on the 5 s ``BOARD_POLL_MS`` poll *and*
    whenever a late boot fetch (``/api/apps``, ``/api/status``) lands. So a
    board test can read across a rebuild at any time, and more often on a
    loaded box, where those fetches land later (#680: measured 3 bad reads in
    700 with every fetch stubbed, at the 5 s cadence; #1346). Auto-retrying
    ``expect()`` assertions re-resolve and are immune; these raw reads are not.
    A script evaluated on a card goes through :func:`stable_eval`, which also
    refuses the detached node such a rebuild leaves behind.

    Returns the first read that isn't one of those artifacts, so the caller
    asserts on a real measurement. Assertion strength is unchanged — only
    known-invalid readings are skipped, and a genuinely wrong value is
    returned as-is on the first attempt.
    """
    value: object = None
    for _ in range(attempts):
        value = read()
        if value not in ("", None):
            return value
        time.sleep(interval_s)
    return value


def stable_eval(locator: Locator, script: str, attempts: int = 50,
                interval_s: float = 0.1) -> object:
    """:func:`stable_read` of ``locator.evaluate(script)`` on the live node only.

    ``script`` is a one-argument JS function (``"el => ..."``). A rebuild that
    lands between Playwright resolving the locator and running the script
    leaves the script a *detached* node (#1346): ``querySelector`` still finds
    its children, but every ``getBoundingClientRect()`` is all zeros and
    ``closest()`` finds no ancestor. That read is a real-looking wrong number
    (a "0px tall" title) or a ``TypeError``, and neither is an artifact
    :func:`stable_read` knows to skip. Checking ``isConnected`` in the same
    JS task as the measurement turns it into ``None``, which is retried
    against the freshly rendered node; nothing can re-render in between.
    """
    guarded = f"(el) => el.isConnected ? ({script})(el) : null"
    return stable_read(lambda: locator.evaluate(guarded), attempts, interval_s)


def assert_unpainted_at_rest(page: Page, verb: Locator) -> None:
    """A row verb is a glyph on nothing, in both themes (fleet-config#1420).

    No fill, no border and no shadow at rest: the owner decision that every
    visible verb on a list row is a plain icon-button next to the kebab. Each
    theme is applied the way the app boots it (``data-theme`` on the root,
    plus the media query) and the check uses ``to_have_css``, which
    re-resolves the locator on every retry, so a poll-driven re-render
    between the two themes cannot hand it a detached node (#680).
    """
    for theme in ("light", "dark"):
        page.emulate_media(color_scheme=theme)
        page.evaluate(
            "t => { document.documentElement.dataset.theme = t; }", theme)
        expect(verb).to_have_css("background-color", "rgba(0, 0, 0, 0)")
        expect(verb).to_have_css("border-top-width", "0px")
        expect(verb).to_have_css("border-bottom-width", "0px")
        expect(verb).to_have_css("box-shadow", "none")


# Bound the default Playwright action + navigation timeout (issue #186).
# Playwright defaults both to 30 s, so a single auto-waiting action whose
# target never settles on a loaded hosted runner — a `.click()` / `goto` /
# `wait_for_selector` with no explicit `timeout=` — blocks the full 30 s as an
# *opaque* wait, and a few stacking inside one test reach the 120 s
# `pytest-timeout` (#184) as a black box that never names which wait hung.
# Capping them well under that deadline turns any such hang into a fast,
# self-naming `TimeoutError: ... waiting for <locator>` instead — diagnosable
# from the run page without a `-v` archaeology dig. `expect()` web-first
# assertions keep their own 5 s default, and any explicit per-call `timeout=`
# still overrides this. Env-tunable like E2E_LOG_POLL_DEADLINE_MS so a slow
# runner can widen it without a code change.
DEFAULT_TIMEOUT_MS = int(os.environ.get("E2E_DEFAULT_TIMEOUT_MS", "15000"))

# Opening the terminal overlay is the single slowest UI operation the suite
# performs (issue #887), and ~19 test modules open it before they can assert
# anything — the hub/voice/summarize readback tests, compose, screenshot
# staging, keys popover, the terminal bar/theme/reconnect/mirror families.
# Every one of them had hardcoded `timeout=10_000`, which is *tighter* than
# the suite's own default just above, for the slowest thing in it. That was
# backwards, and it silently became the gate's dominant failure mode: on a
# loaded dev box the gate failed 8-17 tests per run with a different set each
# time, drawn entirely from this pool, while a clean `main` failed at the
# same rate (#887's control run) — so the gate blocked every ship and taught
# us to wave the reds through.
#
# Measured on the reference dev box, 12 samples per projection, wall time
# from `goto(/?terminal=<sid>)` to `#terminalOverlay:not([hidden])`:
#
#     chromium  p50 3.9 s   max  8.0 s
#     webkit    p50 4.5 s   max 12.2 s   <- already over the old 10 s budget
#
# 30 s is ~2.5x the observed max and ~7x p50, with headroom for a box busier
# than the one measured, while staying well under the 120 s `pytest-timeout`
# (#184) so a genuinely dead overlay still fails fast and *named* rather than
# as a black box — #186's point, which a blanket rise in DEFAULT_TIMEOUT_MS
# would have undone for every action in the suite.
#
# This is deliberately ONE budget shared by the whole pool: the per-leg vars
# E2E_LOG_POLL_DEADLINE_MS (#58/#184), E2E_STOP_OVERLAY_HIDE_MS (#253/#286)
# and E2E_REAL_AGENT_ECHO_MS (#444/#678) each widened exactly one test after
# it became painful, which is how the other ~35 members of the same pool were
# left marginal. Those three stay separate on purpose — they time different
# things (a ConPTY keystroke round-trip, the host's 5 s grace-then-force stop
# window, a real Claude cold boot), not this one.
OVERLAY_OPEN_MS = int(os.environ.get("E2E_OVERLAY_OPEN_MS", "30000"))


# ----------------------------------------------- the boot build readout (#1513)
#
# `#buildReadout` fills from `GET /api/version`, a server round trip that runs
# `git` in a worker thread and probes the session-host, issued alongside the
# rest of boot's reads. Measured under a CPU burn at core count (#1509's
# diagnosis): the call takes ~0.9 s on a quiet box but up to 5.6 s loaded, and
# the readout fills up to 8.3 s after page start. Five tests waited on it with
# a literal 10 s, which a heavily loaded full gate run exceeded; the settings
# test, which also aborts the first two calls (#1287) and so adds the retry
# backoff (500 ms + 1 s) on top, failed most often. At 32 burners on the
# 16-core box the old budget went red in 1 of 4 chromium and 2 of 4 webkit
# runs. 30 s is ~3.6x the loaded max and, like OVERLAY_OPEN_MS, still well
# under the 120 s `pytest-timeout`, so a readout that really never fills fails
# fast and named. One budget for every `#buildReadout` wait, not a literal per
# test.
BUILD_READOUT_MS = int(os.environ.get("E2E_BUILD_READOUT_MS", "30000"))
