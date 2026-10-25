"""Browser, context and page fixtures, and the loopback calls they make."""

from __future__ import annotations

import json
import logging
import re
import urllib3
import warnings
from typing import Iterator, Optional

import pytest
import requests
from playwright.sync_api import BrowserContext, Page

from tests.e2e._shot import CHROMIUM_CAPTURE_ARGS
from tests.e2e.fixtures.env import TOKEN_KEY, autoboot_enabled
from tests.e2e.fixtures.waits import DEFAULT_TIMEOUT_MS

logger = logging.getLogger(__name__)

# The webapp uses a self-signed cert; silence the urllib3 noise from /healthz.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


@pytest.fixture(scope="session")
def browser_context_args(
    browser_context_args: dict, browser_name: str, playwright
) -> dict:
    # Self-signed cert on 8445 — the SPA + service-worker won't load otherwise.
    args = {**browser_context_args, "ignore_https_errors": True}
    if browser_name == "webkit":
        # Project the WebKit engine onto an iPhone 15 Pro Max — viewport,
        # user_agent, has_touch, is_mobile, device_scale_factor — so the suite
        # exercises an iPhone-shaped target on Windows (issue #31).
        args = {**args, **playwright.devices["iPhone 15 Pro Max"]}
    return args


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args: dict, browser_name: str) -> dict:
    """Chromium: accept the loopback cert at the network layer (#1733).

    The webapp serves the tailnet hostname's cert, so on 127.0.0.1 every TLS
    handshake fails its name check. `ignore_https_errors` lets the request
    through, but Chromium never pools a socket whose handshake failed: one page
    boot opened 25 connections instead of 6 (net-log), every Chromium test, and
    at 3 workers the closed sockets peaked at ~10,600 TIME_WAIT, enough to fail
    unrelated navigations with ERR_ADDRESS_IN_USE / ERR_NO_BUFFER_SPACE. With
    the switch the handshake passes and the sockets are reused. WebKit already
    pools them (~6 per boot) and has no such switch.
    """
    if browser_name != "chromium":
        return browser_type_launch_args
    extra = list(browser_type_launch_args.get("args") or [])
    return {**browser_type_launch_args, "args": extra + ["--ignore-certificate-errors", *CHROMIUM_CAPTURE_ARGS]}


# The readout only shows the server's constant `git_sha` and `built_at`, but
# the server answers every call with fresh `git` reads (HEAD, the default
# remote ref and its sha) and a session-host probe: four or five `git` spawns
# per page boot, the largest `git` cost a profiled gate run had left after
# #1512 (#1516). Under autoboot each context serves the first good body this
# worker read. A test's own `page.route` still wins over a context route.
# The body is read here, not by `route.fetch()` from the handler: a fetch
# still in flight when its test's context closed raised `TargetClosedError`
# into the next test's setup.
_VERSION_BODY: dict = {}


@pytest.fixture(autouse=True)
def _cached_version_route(request: pytest.FixtureRequest, context: BrowserContext) -> None:
    if not autoboot_enabled(request.config):
        return
    if "body" not in _VERSION_BODY:
        base = request.getfixturevalue("base_url")
        try:
            res = loopback("GET", f"{base}/api/version")
            res.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("⚠️ /api/version not cached, pages read the server: %s", exc)
            return
        _VERSION_BODY["body"] = res.text
    body = _VERSION_BODY["body"]
    context.route(
        re.compile(r".*/api/version$"),
        lambda route: route.fulfill(status=200, content_type="application/json", body=body),
    )


@pytest.fixture(autouse=True)
def _bound_default_timeouts(context: BrowserContext) -> None:
    # Set on the context, not a single page: authed_page / unauthed_page each
    # `context.new_page()`, and the default is consulted at action time, so a
    # context-level cap covers every page they create.
    context.set_default_timeout(DEFAULT_TIMEOUT_MS)
    context.set_default_navigation_timeout(DEFAULT_TIMEOUT_MS)


def seed_token_init_script(token: str) -> str:
    # Seeded *before* the first navigation so app.js reads it on boot rather
    # than going through the ?token=… URL strip dance (which would leak the
    # token into Playwright trace URLs).
    safe = json.dumps(token)
    safe_key = json.dumps(TOKEN_KEY)
    return f"window.localStorage.setItem({safe_key}, {safe});"


# One pooled session per worker for the per-test loopback calls (#1733): a
# bare requests.request opened and closed a connection each, two or three
# per test, every one a TIME_WAIT socket.
_LOOPBACK_SESSION = requests.Session()


def loopback(method: str, url: str, **kwargs: object) -> requests.Response:
    """One request to the disposable webapp's self-signed loopback cert,
    without the InsecureRequestWarning pytest re-arms for every test."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
        return _LOOPBACK_SESSION.request(method, url, timeout=10, verify=False, **kwargs)


@pytest.fixture(autouse=True)
def _restore_disposable_config(request: pytest.FixtureRequest) -> Iterator[None]:
    """Put back any server-side setting a test changed (#1231).

    Several tests POST /api/config (favourite agent, hidden agents, model)
    and leave the value behind; run in file order, a sibling test in the same
    module happened to reset it. Spread over parallel workers the sibling can
    run on another worker's webapp, so the change leaked: a favourite set to
    Codex left `test_resume_toggle` waiting for a Claude main button that no
    longer existed. The webapp holds its config in memory (`app.state`), so
    restoring means POSTing the changed keys back; the endpoint drops any key
    it does not accept. Autoboot only — never touch the live tray's config.
    """
    if "base_url" not in request.fixturenames or not autoboot_enabled(request.config):
        yield
        return
    base = request.getfixturevalue("base_url")

    def _snapshot() -> Optional[dict]:
        try:
            res = loopback("GET", f"{base}/api/config")
            res.raise_for_status()
            return res.json()
        except requests.RequestException as exc:
            logger.warning("⚠️ could not snapshot the disposable config: %s", exc)
            return None

    before = _snapshot()
    yield
    after = _snapshot() if before is not None else None
    if before is None or after is None:
        return
    changed = {k: v for k, v in before.items() if after.get(k) != v}
    if changed:
        try:
            loopback("POST", f"{base}/api/config", json=changed).raise_for_status()
        except requests.RequestException as exc:
            logger.warning("⚠️ could not restore the disposable config %s: %s",
                           sorted(changed), exc)

@pytest.fixture
def authed_page(
    context: BrowserContext, base_url: str, auth_token: str
) -> Iterator[Page]:
    if auth_token:
        context.add_init_script(seed_token_init_script(auth_token))
    # The header's CPU reading (#1522) reads the real box, and the gate loads
    # it: a hot reading shows beside the header line and would make every
    # header-width test depend on the gate's own load. A quiet default; a
    # page.route() in a test takes precedence over this context route.
    context.route(
        re.compile(r".*/api/system/load$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=_QUIET_SYSTEM_LOAD
        ),
    )
    page = context.new_page()
    try:
        yield page
    finally:
        page.close()


_QUIET_SYSTEM_LOAD = json.dumps({
    "level": "normal", "reason": None, "cpu_percent": 12.0,
    "memory": {"used_bytes": 8 * 1024**3, "total_bytes": 32 * 1024**3, "percent": 25.0},
    "top": [], "cores": 8, "age_seconds": 1.0,
})


@pytest.fixture
def unauthed_page(context: BrowserContext) -> Iterator[Page]:
    page = context.new_page()
    try:
        yield page
    finally:
        page.close()
