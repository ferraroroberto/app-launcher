"""Fixtures for the Playwright smoke suite.

Two run modes:

* **Live (dev loop).** Runs against a live tray the user already has up on
  https://127.0.0.1:8445 — but only with the explicit `LAUNCHER_E2E_LIVE=1`
  opt-in (`scripts/run-e2e.ps1` sets it). Without it the suite *exits* with a
  guard message instead of running: a bare `pytest tests/e2e` used to silently
  load-test the instance the phone was using (issue #386). The autouse
  `_require_live_tray` fixture still skips the whole module with a clear
  message if /healthz isn't reachable, so a forgotten tray fails fast instead
  of hanging in browser.goto for 30 s.
* **Autoboot (pre-ship gate).** Enabled with `--e2e-autoboot` or the
  `LAUNCHER_E2E_AUTOBOOT=1` env var. `_autoboot_server` spawns a disposable
  webapp on a free port (HTTPS, reusing webapp/certificates/) plus its own
  disposable session-host on a free port — it never adopts a host already
  listening on the live :8446 (issue #260). In this mode a failure to boot is
  a hard *failure*, never a skip: the whole point of the gate is that a
  missing server can't silently pass. See issue #33.

`pytest_sessionfinish` runs the vendor-verbatim leaked-browser-helper sweep
(`tests/e2e/_browser_sweep.py`, project-scaffolding #203/#204) once the whole
session — fixtures included — has torn down, so a run that orphaned a WebKit
helper reclaims it *while it is still killable*, instead of leaving one
pinning this checkout's directory (which is what makes a later `git worktree
remove` fail as "busy"). See issue #709.
"""

from __future__ import annotations

import logging
import os
from typing import Iterator

import pytest
import requests

from tests._credential_hygiene import disposable_token, register_secret
from tests.e2e._browser_sweep import sweep_browser_helpers
# Tree-aware stop of the disposable servers + the stub-child reap (#1335).
from tests.e2e._process_teardown import reap_stub_children
# Design-inventory capture (#1698): the vendored surface() hook's collection
# filter, aliased because this file already defines its own hook below.
from tests.e2e._surface import pytest_collection_modifyitems as _surface_collection_filter
from tests.e2e.fixtures.autoboot import run_autoboot
from tests.e2e.fixtures.env import BASE_URL, LIVE_ENV, REPO_ROOT, autoboot_enabled
# The fixtures below are defined in tests/e2e/fixtures/ and registered here, where
# pytest looks for them.
from tests.e2e.fixtures.pages import (
    _bound_default_timeouts,
    _cached_version_route,
    _restore_disposable_config,
    authed_page,
    browser_context_args,
    browser_type_launch_args,
    unauthed_page,
)
from tests.e2e.fixtures.sessions import (
    launched_claude_pty_session,
    launched_pty_session,
    session_log_path,
    wait_for_session_log,
)

__all__ = [
    "_bound_default_timeouts",
    "_cached_version_route",
    "_restore_disposable_config",
    "authed_page",
    "browser_context_args",
    "browser_type_launch_args",
    "launched_claude_pty_session",
    "launched_pty_session",
    "session_log_path",
    "unauthed_page",
    "wait_for_session_log",
]

logger = logging.getLogger(__name__)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--e2e-autoboot",
        action="store_true",
        default=False,
        help="Boot a disposable webapp + session-host instead of requiring a "
        "live tray. Equivalent to LAUNCHER_E2E_AUTOBOOT=1.",
    )


@pytest.fixture(scope="session")
def base_url(request: pytest.FixtureRequest) -> str:
    if autoboot_enabled(request.config):
        return request.getfixturevalue("_autoboot_server")
    return BASE_URL


@pytest.fixture(scope="session")
def auth_token() -> str:
    """This run's disposable bearer token — never the live one (issue #907).

    Loopback bypasses the bearer middleware (``BearerTokenMiddleware``) and
    every WS token gate, so no e2e test needs the real credential — against
    the disposable autoboot webapp (whose config carries this same token) or
    the live tray alike. It is still seeded so the SPA boot path mirrors a
    real phone session, and registered for redaction so even this throwaway
    value stays out of failure output.
    """
    token = disposable_token()
    register_secret(token)
    return token


@pytest.fixture(scope="session", autouse=True)
def _require_live_tray(request: pytest.FixtureRequest, base_url: str) -> None:
    # Under autoboot the disposable server is already up — `_autoboot_server`
    # hard-fails if it isn't, so the skip-guard below would be wrong there.
    # The guard only protects the default ad-hoc path against a forgotten tray.
    if autoboot_enabled(request.config):
        return
    if os.environ.get(LIVE_ENV, "") != "1":
        pytest.exit(
            "Refusing to run the e2e suite against the LIVE tray on :8445 "
            "without explicit opt-in (issue #386) — an ad-hoc run load-tests "
            "the instance the phone is using. Either set LAUNCHER_E2E_LIVE=1 "
            "(scripts/run-e2e.ps1 does) to target the live tray on purpose, "
            "or use the disposable autoboot mode: --e2e-autoboot / "
            "LAUNCHER_E2E_AUTOBOOT=1.",
            returncode=2,
        )
    try:
        res = requests.get(f"{base_url}/healthz", timeout=2, verify=False)
        res.raise_for_status()
    except Exception as exc:
        pytest.skip(
            f"Tray not running on 8445 ({exc.__class__.__name__}) — "
            "start tray.bat first, then re-run the suite."
        )


def pytest_configure(config: pytest.Config) -> None:
    # Default the e2e suite to dual projections (Chromium-desktop + WebKit-iPhone)
    # when --browser wasn't passed, so WebKit coverage is impossible to forget
    # (issue #31). Users can still pin a single engine with `--browser chromium`
    # for a faster dev loop; pytest-playwright treats --browser as append-style.
    selected = config.option.browser
    if not selected:
        selected.extend(["chromium", "webkit"])


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Sweep browser helpers this run orphaned inside *this* checkout (#709).

    A session hook, not a fixture finalizer: it must run after *every* fixture
    — including pytest-playwright's own session-scoped `browser` — has already
    torn down, or the sweep would be looking at a browser that is still
    legitimately running. The scope path is the only call-site argument, so
    `_browser_sweep.py` stays byte-identical to project-scaffolding's copy.

    Advisory by design: it reports and never touches `exitstatus`, because an
    already-exited handle-held zombie is unkillable and is not a test failure
    (see `_browser_sweep`'s module docstring for why those exist, and why
    nothing is ever killed by image name alone — Chromium is deliberately out
    of the sweep set, which matters on a box where the user's own Chrome is
    always up).
    """
    result = sweep_browser_helpers(REPO_ROOT)
    print(f"\n{result.summary()}")
    for entry in result.killed:
        print(f"  reclaimed leaked helper: {entry}")
    # Stub children (#1335): `_autoboot_server`'s own teardown reaps them, but
    # a worker that died before its finalizers ran never gets there. Every
    # stub script this run wrote sits under its pytest basetemp (a worker's
    # `popen-gwN` sits under the controller's), so the scope is this run and
    # nothing else. Advisory, like the browser sweep.
    try:
        for leaked_stub in reap_stub_children(session.config._tmp_path_factory.getbasetemp()):
            print(f"  reclaimed leaked e2e stub child: {leaked_stub}")
    except RuntimeError as exc:
        print(f"  ⚠️ {exc}")


def pytest_collection_modifyitems(config: pytest.Config, items: list) -> None:
    """Run the WebKit/iPhone projection only where engine or viewport matters.

    The one place that decides it (#1220). A browser test runs on Chromium,
    and also on WebKit/iPhone only when it carries the ``iphone`` marker
    (``pytest.ini``), on the test or in its module's ``pytestmark``. Keep
    ``iphone`` for layout, geometry and computed style, touch targets, nav,
    the composer and keyboard input, safe-area, and tests that branch on the
    engine. Everything else gives the same answer on both engines, so its
    WebKit node is *deselected* here rather than skipped: it is never built,
    and never counted as a skip.

    ``tests/unit/repo/test_e2e_iphone_projection.py`` keeps the marker honest: any test
    that reads geometry or computed style, taps, or branches on the engine
    must carry it. Measured basis (#1220): 0 WebKit-engine product bugs in
    400 PRs, WebKit was 61% of browser time, and iOS-only bugs don't
    reproduce in desktop WebKit either, so the real iPhone check stays on the
    device. Chromium is the engine kept everywhere because the gate's narrow
    static tier (#568) runs Chromium only.
    """
    kept, dropped = [], []
    for item in items:
        callspec = getattr(item, "callspec", None)
        on_webkit = callspec is not None and callspec.params.get("browser_name") == "webkit"
        if on_webkit and item.get_closest_marker("iphone") is None:
            dropped.append(item)
        else:
            kept.append(item)
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept
    # Inert unless DESIGN_SURFACE_CAPTURE is set; then it keeps only the tests
    # that call surface(). Must follow the projection filter above.
    _surface_collection_filter(config, items)



@pytest.fixture(scope="session")
def _autoboot_server(
    tmp_path_factory: pytest.TempPathFactory, auth_token: str
) -> Iterator[str]:
    """Spawn a disposable webapp (+ session-host) and yield its base URL.

    A hard failure (`pytest.fail`) — never a skip — if anything doesn't come
    up: under the pre-ship gate a missing server must not pass silently.
    """
    yield from run_autoboot(tmp_path_factory, auth_token)
