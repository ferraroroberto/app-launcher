"""Fixtures for the Playwright smoke suite.

Two run modes:

* **Default (ad-hoc).** Runs against a live tray the user already has up on
  https://127.0.0.1:8445. The autouse `_require_live_tray` fixture skips the
  whole module with a clear message if /healthz isn't reachable, so a
  forgotten tray fails fast instead of hanging in browser.goto for 30 s.
* **Autoboot (pre-ship gate).** Enabled with `--e2e-autoboot` or the
  `LAUNCHER_E2E_AUTOBOOT=1` env var. `_autoboot_server` spawns a disposable
  webapp on a free port (HTTPS, reusing webapp/certificates/) plus a
  session-host on :8446 — adopting an already-listening one (a running tray)
  or spawning its own. In this mode a failure to boot is a hard *failure*,
  never a skip: the whole point of the gate is that a missing server can't
  silently pass. See issue #33.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib3
from pathlib import Path
from typing import Callable, IO, Iterator, List, Optional

import pytest
import requests
from playwright.sync_api import BrowserContext, Page

logger = logging.getLogger(__name__)

# The webapp uses a self-signed cert; silence the urllib3 noise from /healthz.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WEBAPP_CONFIG = _REPO_ROOT / "config" / "webapp_config.json"
_SESSIONS_DIR = _REPO_ROOT / "webapp" / "sessions"
_BASE_URL = "https://127.0.0.1:8445"
_TOKEN_KEY = "launcher.token"  # must match TOKEN_KEY in app/webapp/static/state.js:21

# The loopback PTY session-host port. Default in webapp_config; the webapp
# subprocess reads it from there, so autoboot keeps the session-host on this
# fixed port (adopt-or-spawn) rather than a free one — no config injection.
_SESSION_HOST_PORT = 8446
_AUTOBOOT_ENV = "LAUNCHER_E2E_AUTOBOOT"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--e2e-autoboot",
        action="store_true",
        default=False,
        help="Boot a disposable webapp + session-host instead of requiring a "
        "live tray. Equivalent to LAUNCHER_E2E_AUTOBOOT=1.",
    )


def _autoboot_enabled(config: pytest.Config) -> bool:
    return bool(config.getoption("--e2e-autoboot")) or (
        os.environ.get(_AUTOBOOT_ENV, "") == "1"
    )


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _port_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _spawn(cmd: List[str], log: IO[str]) -> subprocess.Popen:
    kwargs: dict = dict(
        cwd=str(_REPO_ROOT),
        stdout=log,
        stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
    )
    if sys.platform == "win32":
        # New process group so we can deliver CTRL_BREAK for a clean stop;
        # no window so the test run doesn't flash consoles.
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    return subprocess.Popen(cmd, **kwargs)


def _terminate(proc: Optional[subprocess.Popen]) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            except Exception as exc:  # pragma: no cover - best effort
                logger.debug("CTRL_BREAK_EVENT failed: %s", exc)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning("⚠️  autoboot: process teardown failed: %s", exc)


def _wait_port(port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_listening(port):
            return True
        time.sleep(0.3)
    return False


def _wait_healthz(base: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            res = requests.get(f"{base}/healthz", timeout=2, verify=False)
            if res.status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.4)
    return False


@pytest.fixture(scope="session")
def _autoboot_server() -> Iterator[str]:
    """Spawn a disposable webapp (+ session-host) and yield its base URL.

    A hard failure (`pytest.fail`) — never a skip — if anything doesn't come
    up: under the pre-ship gate a missing server must not pass silently.
    """
    from app.webapp.manager import cert_paths

    logs_dir = _REPO_ROOT / "webapp"  # gitignored runtime dir
    logs_dir.mkdir(parents=True, exist_ok=True)
    handles: List[IO[str]] = []
    sh_proc: Optional[subprocess.Popen] = None
    wa_proc: Optional[subprocess.Popen] = None

    def _open_log(name: str) -> IO[str]:
        handle = (logs_dir / name).open("w", encoding="utf-8", errors="replace")
        handles.append(handle)
        return handle

    def _teardown() -> None:
        _terminate(wa_proc)
        if sh_proc is not None:  # only the one we own — never an adopted tray
            _terminate(sh_proc)
        for handle in handles:
            try:
                handle.close()
            except Exception:  # pragma: no cover
                pass

    try:
        # Session-host: adopt an already-listening one (a running tray), else
        # spawn our own on the same fixed port the webapp config expects.
        if not _port_listening(_SESSION_HOST_PORT):
            sh_cmd = [
                sys.executable,
                str(_REPO_ROOT / "launcher.py"),
                "session-host",
                "--port",
                str(_SESSION_HOST_PORT),
            ]
            sh_proc = _spawn(sh_cmd, _open_log("e2e-autoboot-session-host.log"))
            if not _wait_port(_SESSION_HOST_PORT, timeout=15):
                _teardown()
                pytest.fail(
                    f"autoboot: session-host did not listen on :{_SESSION_HOST_PORT} "
                    "within 15s — see webapp/e2e-autoboot-session-host.log"
                )

        # Webapp on a free port. HTTPS when the cert pair exists (mirrors the
        # real phone path); plain HTTP otherwise so a cert-less checkout still
        # runs the gate.
        port = _free_tcp_port()
        certs = cert_paths()
        scheme = "https" if certs else "http"
        wa_cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            "app.webapp.server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ]
        if certs:
            cert, key = certs
            wa_cmd += ["--ssl-keyfile", str(key), "--ssl-certfile", str(cert)]
        wa_proc = _spawn(wa_cmd, _open_log("e2e-autoboot-webapp.log"))

        base = f"{scheme}://127.0.0.1:{port}"
        if not _wait_healthz(base, timeout=20):
            _teardown()
            pytest.fail(
                f"autoboot: webapp did not answer /healthz at {base} within 20s "
                "— see webapp/e2e-autoboot-webapp.log"
            )
        logger.info("✅ autoboot: webapp ready at %s", base)
        yield base
    finally:
        _teardown()


@pytest.fixture(scope="session")
def base_url(request: pytest.FixtureRequest) -> str:
    if _autoboot_enabled(request.config):
        return request.getfixturevalue("_autoboot_server")
    return _BASE_URL


@pytest.fixture(scope="session")
def webapp_config() -> dict:
    if not _WEBAPP_CONFIG.exists():
        pytest.skip(f"{_WEBAPP_CONFIG} missing — copy webapp_config.sample.json first")
    return json.loads(_WEBAPP_CONFIG.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def auth_token(webapp_config: dict) -> str:
    # Loopback bypasses the bearer middleware (server.py:267), so an empty
    # token is fine for local-only tests. We still seed it when present so
    # the SPA boot path mirrors a real phone session.
    return (webapp_config.get("auth_token") or "").strip()


@pytest.fixture(scope="session", autouse=True)
def _require_live_tray(request: pytest.FixtureRequest, base_url: str) -> None:
    # Under autoboot the disposable server is already up — `_autoboot_server`
    # hard-fails if it isn't, so the skip-guard below would be wrong there.
    # The guard only protects the default ad-hoc path against a forgotten tray.
    if _autoboot_enabled(request.config):
        return
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
_DEFAULT_TIMEOUT_MS = int(os.environ.get("E2E_DEFAULT_TIMEOUT_MS", "15000"))


@pytest.fixture(autouse=True)
def _bound_default_timeouts(context: BrowserContext) -> None:
    # Set on the context, not a single page: authed_page / unauthed_page each
    # `context.new_page()`, and the default is consulted at action time, so a
    # context-level cap covers every page they create.
    context.set_default_timeout(_DEFAULT_TIMEOUT_MS)
    context.set_default_navigation_timeout(_DEFAULT_TIMEOUT_MS)


def _seed_token_init_script(token: str) -> str:
    # Seeded *before* the first navigation so app.js reads it on boot rather
    # than going through the ?token=… URL strip dance (which would leak the
    # token into Playwright trace URLs).
    safe = json.dumps(token)
    safe_key = json.dumps(_TOKEN_KEY)
    return f"window.localStorage.setItem({safe_key}, {safe});"


@pytest.fixture
def authed_page(
    context: BrowserContext, base_url: str, auth_token: str
) -> Iterator[Page]:
    if auth_token:
        context.add_init_script(_seed_token_init_script(auth_token))
    page = context.new_page()
    try:
        yield page
    finally:
        page.close()


@pytest.fixture
def unauthed_page(context: BrowserContext) -> Iterator[Page]:
    page = context.new_page()
    try:
        yield page
    finally:
        page.close()


# ---------------------------------------------------------------- session API
# Opt-in fixture: tests that need state in #sessionsList depend on this; other
# tests don't pay the 3-5 s launch + teardown cost. Target is `app-launcher`
# itself (self-launching is harmless — just spawns claude in this repo dir).

_LAUNCH_TARGET_ID = "app-launcher"


def _auth_headers(auth_token: str) -> dict:
    return {"Authorization": f"Bearer {auth_token}"} if auth_token else {}


def _stop_session(base_url: str, headers: dict, sid: str) -> None:
    """Force-kill a PTY session. `mode: "kill"` is unconditional (vs "quit",
    which waits for claude to process /quit). Best-effort — a swallowed
    exception here must not mask the actual test failure."""
    try:
        requests.post(
            f"{base_url}/api/claude-code/sessions/{sid}/stop",
            json={"mode": "kill", "close_window": True},
            headers=headers,
            verify=False,
            timeout=5,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  session %s teardown failed: %s", sid, exc)


@pytest.fixture
def launched_pty_session(base_url: str, auth_token: str) -> Iterator[str]:
    # The PTY session runs a real `claude` process. Where `claude` isn't on
    # PATH — notably the CI runner, which never installs it — the PTY child
    # exits at once ("'claude' is not recognized…"), the session-host reaps
    # it, and its WS endpoint then 403s the webapp's proxy, so every
    # input-delivery test below would race a corpse. Skip cleanly instead:
    # these tests genuinely gate on a dev box where `claude` runs. The test
    # process shares the session-host's PATH (same machine), so `which` here
    # faithfully predicts whether the session-host can spawn it. See #58.
    if shutil.which("claude") is None:
        pytest.skip(
            "`claude` is not on PATH — terminal input-delivery tests need a "
            "live claude PTY and skip cleanly where it isn't installed (e.g. "
            "the CI runner)"
        )

    headers = _auth_headers(auth_token)
    try:
        res = requests.post(
            f"{base_url}/api/apps/{_LAUNCH_TARGET_ID}/launch",
            json={"mode": "pty"},
            headers=headers,
            verify=False,
            timeout=10,
        )
    except Exception as exc:
        pytest.skip(f"launch request failed: {exc.__class__.__name__}: {exc}")

    if res.status_code != 200:
        # 400 is the expected failure when the project_dir is invalid — skip
        # cleanly rather than fail the suite.
        pytest.skip(f"could not launch PTY session (HTTP {res.status_code}: {res.text[:200]})")

    body = res.json()
    sid = body.get("session", {}).get("session_id")
    if not sid:
        pytest.skip(f"launch response missing session_id: {body}")

    try:
        yield sid
    finally:
        _stop_session(base_url, headers, sid)


# ----------------------------------------------------- input-delivery polling
# Env-aware so the slow hosted CI runner gets headroom without slowing local
# runs (issue #184, finishing #58): the ConPTY round-trip (keystroke → session
# host → log flush) lands well within 5 s locally but can exceed it on a loaded
# windows-2025 runner. e2e.yml sets E2E_LOG_POLL_DEADLINE_MS larger for CI.
_LOG_POLL_DEADLINE_MS = int(os.environ.get("E2E_LOG_POLL_DEADLINE_MS", "5000"))


@pytest.fixture
def wait_for_session_log() -> Callable[..., bool]:
    """Return a poller for the per-session input log.

    ``wait(page, sid, needle, deadline_ms=_LOG_POLL_DEADLINE_MS)`` reads
    ``webapp/sessions/<sid>.log`` every 200 ms until ``needle`` appears or the
    deadline elapses, then returns ``True``/``False``. One source of truth for
    the input-delivery wait that used to be a hardcoded 5 s poll loop copied
    into four test files (issue #58).
    """

    def _wait(
        page: Page,
        sid: str,
        needle: str,
        deadline_ms: int = _LOG_POLL_DEADLINE_MS,
    ) -> bool:
        log_path = _SESSIONS_DIR / f"{sid}.log"

        def _hit() -> bool:
            return log_path.exists() and needle in log_path.read_text(
                encoding="utf-8", errors="replace"
            )

        for _ in range(max(1, deadline_ms // 200)):
            if _hit():
                return True
            page.wait_for_timeout(200)
        # Final read so a hit landing in the last 200 ms interval isn't missed.
        return _hit()

    return _wait
