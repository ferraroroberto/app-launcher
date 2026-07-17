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

# The live tray's loopback PTY session-host port. Autoboot must NEVER adopt a
# host listening here: on a dev box it holds the user's real PTY/Claude
# sessions — including the one running the gate — and the destructive e2e
# tests would kill them (issue #260). Instead autoboot always spawns its own
# disposable session-host on a free port and points the disposable webapp at
# it via LAUNCHER_SESSION_HOST_PORT. This constant is kept only so the
# isolation guarantee can be asserted (never bound/adopted under autoboot).
_LIVE_SESSION_HOST_PORT = 8446
# Env var the webapp honours to override its session-host port (see
# src/webapp_config.py:SESSION_HOST_PORT_ENV) — the injection that isolates
# the gate from the live :8446.
_SESSION_HOST_PORT_ENV = "LAUNCHER_SESSION_HOST_PORT"
# Env var the webapp honours to override its config file *path* (see
# src/webapp_config.py:WEBAPP_CONFIG_PATH_ENV) — the injection that stops a
# Settings-tab e2e Save from ever mutating the user's real
# config/webapp_config.json (issue #441; the #438 port corruption was this
# exact shared-file design biting). Autoboot points the disposable webapp at
# a temp COPY of the real config so it still boots with realistic values.
_WEBAPP_CONFIG_PATH_ENV = "LAUNCHER_WEBAPP_CONFIG"
_AUTOBOOT_ENV = "LAUNCHER_E2E_AUTOBOOT"
# Sentinel flag for the lightweight PTY child (issue #534). Under autoboot the
# disposable session-host's PATH is prepended with a harness-generated
# `claude.cmd` shim: a launch whose flags are exactly this sentinel runs a
# tiny deterministic Python echo loop instead of the real Claude CLI (3-5 s
# startup each), while any other flag set falls through to the real `claude`.
# Purely a harness substitution — no production code knows about it.
_STUB_FLAG = "--e2e-stub"
# Filled by _autoboot_server so the lightweight fixture can create sessions
# directly on the disposable session-host (the sentinel flag can't travel
# through the webapp's launch endpoint, which builds flags from config).
_AUTOBOOT_STATE: dict = {}
# Explicit opt-in for targeting the LIVE tray on :8445 (issue #386). The
# live mode is deliberate (run-e2e.ps1 dev loop), but it drives real login
# flows and PTY sessions against the instance the user's phone is using —
# an *accidental* bare `pytest tests/e2e` must not do that.
_LIVE_ENV = "LAUNCHER_E2E_LIVE"


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


def _spawn(
    cmd: List[str],
    log: IO[str],
    extra_env: Optional[dict] = None,
) -> subprocess.Popen:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    if extra_env:
        env.update(extra_env)
    kwargs: dict = dict(
        cwd=str(_REPO_ROOT),
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
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


_STUB_CHILD_SOURCE = '''\
"""Deterministic lightweight PTY child for UI-only e2e tests (issue #534).

Stands in for the real Claude CLI under the disposable autoboot session-host:
instant startup, echoes each input line (ConPTY cooked mode echoes keystrokes
too), exits on /quit so the host's graceful stop path works.
"""
import sys

print("[e2e-stub] lightweight PTY child ready (issue #534)", flush=True)
while True:
    line = sys.stdin.readline()
    if not line:
        break
    text = line.rstrip("\\r\\n")
    if text.strip() == "/quit":
        print("[e2e-stub] bye", flush=True)
        break
    print(text, flush=True)
'''


def _write_claude_shim(shim_dir: Path) -> None:
    """Generate the `claude.cmd` PATH shim + stub child script (issue #534).

    The session-host spawns agents via ``cmd /c … && claude <flags>`` with the
    command resolved off its own PATH, so prepending this directory to the
    *disposable* session-host's PATH intercepts every claude launch: the
    ``--e2e-stub`` sentinel routes to the stub child, anything else falls
    through to the real ``claude`` resolved at generation time. Where claude
    isn't installed (the CI runner) the fall-through branch fails loud — but
    it is never reached there, because `launched_claude_pty_session` skips
    first (same `shutil.which` guard as always).
    """
    stub_py = shim_dir / "e2e_stub_child.py"
    stub_py.write_text(_STUB_CHILD_SOURCE, encoding="utf-8")
    real_claude = shutil.which("claude")
    if real_claude:
        real_branch = f'call "{real_claude}" %*\nexit /b %ERRORLEVEL%\n'
    else:
        real_branch = (
            "echo [e2e-shim] real claude is not installed 1>&2\n"
            "exit /b 1\n"
        )
    shim = (
        "@echo off\n"
        f'if "%~1"=="{_STUB_FLAG}" (\n'
        f'  "{sys.executable}" -X utf8 "{stub_py}"\n'
        "  exit /b %ERRORLEVEL%\n"
        ")\n"
        f"{real_branch}"
    )
    # Text-mode write translates \n -> os.linesep, so the .cmd lands with
    # proper CRLF line endings on Windows.
    (shim_dir / "claude.cmd").write_text(shim, encoding="ascii")


@pytest.fixture(scope="session")
def _autoboot_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Spawn a disposable webapp (+ session-host) and yield its base URL.

    A hard failure (`pytest.fail`) — never a skip — if anything doesn't come
    up: under the pre-ship gate a missing server must not pass silently.
    """
    from app.webapp.event_loop import LOOP_FACTORY
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
        if sh_proc is not None:  # always ours now (never an adopted tray)
            _terminate(sh_proc)
        for handle in handles:
            try:
                handle.close()
            except Exception:  # pragma: no cover
                pass

    # Config isolation (issue #441): the disposable webapp gets a temp COPY
    # of the real config — realistic values (projects_dir, auth_token, …)
    # without write access to the real file. Any e2e test that Saves settings
    # mutates only the copy. Snapshot the real file's bytes so the isolation
    # can be *asserted* after the run, not just assumed.
    cfg_copy = logs_dir / "e2e-autoboot-webapp-config.json"
    real_cfg_bytes = (
        _WEBAPP_CONFIG.read_bytes() if _WEBAPP_CONFIG.exists() else None
    )
    if real_cfg_bytes is not None:
        cfg_copy.write_bytes(real_cfg_bytes)
    elif cfg_copy.exists():
        # No real config (fresh checkout) — a stale copy from a prior run
        # must not leak its values into this one.
        cfg_copy.unlink()

    try:
        # Session-host: ALWAYS spawn our own on a free port — never adopt a
        # host already listening on the live :8446, which on a dev box owns
        # the user's real PTY/Claude sessions (issue #260). A free, disposable
        # host starts empty, so the destructive e2e tests can only ever touch
        # sessions this run launched. The disposable webapp is pointed at it
        # via LAUNCHER_SESSION_HOST_PORT below.
        sh_port = _free_tcp_port()
        sh_cmd = [
            sys.executable,
            str(_REPO_ROOT / "launcher.py"),
            "session-host",
            "--port",
            str(sh_port),
        ]
        # Lightweight-child shim (issue #534): only the DISPOSABLE
        # session-host gets the shim on PATH — the pytest process and the
        # live tray keep the real resolution, so `shutil.which("claude")`
        # in the fixtures below still faithfully predicts the real CLI.
        shim_dir = tmp_path_factory.mktemp("claude-shim")
        _write_claude_shim(shim_dir)
        sh_proc = _spawn(
            sh_cmd,
            _open_log("e2e-autoboot-session-host.log"),
            extra_env={"PATH": f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
        )
        _AUTOBOOT_STATE["session_host_port"] = sh_port
        if not _wait_port(sh_port, timeout=15):
            _teardown()
            pytest.fail(
                f"autoboot: session-host did not listen on :{sh_port} "
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
            "--loop",
            LOOP_FACTORY,
        ]
        if certs:
            cert, key = certs
            wa_cmd += ["--ssl-keyfile", str(key), "--ssl-certfile", str(cert)]
        # Point the disposable webapp at our disposable session-host, not the
        # config's :8446, and at the temp config copy, not the real file —
        # the two env injections that isolate the gate (issues #260, #441).
        wa_proc = _spawn(
            wa_cmd,
            _open_log("e2e-autoboot-webapp.log"),
            extra_env={
                _SESSION_HOST_PORT_ENV: str(sh_port),
                _WEBAPP_CONFIG_PATH_ENV: str(cfg_copy),
            },
        )

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
        # Isolation regression check (issue #441): the real config must be
        # byte-identical to the pre-run snapshot. A mismatch means some path
        # wrote to the real file during the gate — the exact class of bug
        # that corrupted session_host_port in #438 — or, rarely, that the
        # user saved settings on the LIVE tray mid-run. Loud either way.
        current = (
            _WEBAPP_CONFIG.read_bytes() if _WEBAPP_CONFIG.exists() else None
        )
        if current != real_cfg_bytes:
            raise RuntimeError(
                f"e2e autoboot isolation breach: {_WEBAPP_CONFIG} changed "
                "during the run. The disposable webapp must only ever write "
                f"its temp copy ({cfg_copy}). If you changed settings on the "
                "live tray while the gate ran, rerun the gate; otherwise a "
                "test wrote to the real config — fix that before shipping."
            )


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
    if os.environ.get(_LIVE_ENV, "") != "1":
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
# Opt-in fixtures: tests that need state in #sessionsList depend on one of
# these; other tests don't pay any launch + teardown cost. Target is
# `app-launcher` itself (self-launching is harmless — just spawns the agent
# in this repo dir).

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
            json={"mode": "kill"},
            headers=headers,
            verify=False,
            timeout=5,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  session %s teardown failed: %s", sid, exc)


def _launch_claude_via_webapp(base_url: str, auth_token: str) -> str:
    """Launch a REAL claude PTY session through the webapp's launch endpoint.

    Where `claude` isn't on PATH — notably the CI runner, which never
    installs it — the PTY child exits at once ("'claude' is not
    recognized…"), the session-host reaps it, and its WS endpoint then 403s
    the webapp's proxy, so every consumer would race a corpse. Skip cleanly
    instead: these tests genuinely gate on a dev box where `claude` runs.
    The test process shares the live session-host's PATH (same machine), so
    `which` here faithfully predicts whether the session-host can spawn it.
    See #58.
    """
    if shutil.which("claude") is None:
        pytest.skip(
            "`claude` is not on PATH — real-Claude PTY tests need a live "
            "claude CLI and skip cleanly where it isn't installed (e.g. the "
            "CI runner)"
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
    return str(sid)


@pytest.fixture
def launched_pty_session(
    request: pytest.FixtureRequest, base_url: str, auth_token: str
) -> Iterator[str]:
    """A live PTY session for UI-only assertions (issue #534).

    Under autoboot (the pre-ship gate + CI) the child is the deterministic
    lightweight stub, created directly on the disposable session-host with
    the ``--e2e-stub`` sentinel — no real Claude CLI process per test. The
    launch API never blocked on Claude's bootstrap, so the win is not big
    idle-box wall time (measured ~20 s across the whole gate, #534): it is
    removing ~110 background node boots whose CPU contention made loaded
    runs balloon, plus CI coverage (the stub needs only Python). The
    production webapp ↔ session-host ↔ ConPTY boundary stays fully real
    (session rows, WS streaming, input forwarding, stop paths).

    Against the LIVE tray (run-e2e.ps1 dev loop) there is no shim on the
    tray's PATH, so this falls back to a real claude launch — behaviour
    identical to before the split.

    Tests that assert real agent semantics (rendered Claude output, agent
    echo, lifecycle) must use `launched_claude_pty_session` instead.
    """
    headers = _auth_headers(auth_token)
    if _autoboot_enabled(request.config):
        sh_port = _AUTOBOOT_STATE.get("session_host_port")
        if not sh_port:
            pytest.fail("autoboot state missing session_host_port (issue #534)")
        # POST the session-host directly: the sentinel flag can't travel
        # through the webapp's launch endpoint (flags come from config
        # there). The session still surfaces through the webapp normally —
        # its session list proxies this same host.
        res = requests.post(
            f"http://127.0.0.1:{sh_port}/sessions",
            json={
                "project_dir": str(_REPO_ROOT),
                "name": _LAUNCH_TARGET_ID,
                "flags": _STUB_FLAG,
                "agent": "claude",
            },
            timeout=15,
        )
        # Deterministic path — a failure here is a harness bug, never a
        # missing-dependency skip.
        if res.status_code != 200:
            pytest.fail(
                f"lightweight stub session failed to launch (HTTP "
                f"{res.status_code}: {res.text[:200]})"
            )
        sid = str(res.json().get("session_id") or "")
        if not sid:
            pytest.fail(f"stub session response missing session_id: {res.text[:200]}")
    else:
        sid = _launch_claude_via_webapp(base_url, auth_token)

    try:
        yield sid
    finally:
        _stop_session(base_url, headers, sid)


@pytest.fixture
def launched_claude_pty_session(base_url: str, auth_token: str) -> Iterator[str]:
    """A live PTY session running the REAL Claude CLI (issue #534).

    Only for tests whose assertions depend on the real agent — rendered
    Claude output in the xterm buffer, agent input echo, Claude lifecycle
    semantics. Spawns a real node process per test: keep its consumer set
    minimal, and put UI-only assertions on `launched_pty_session`.
    """
    headers = _auth_headers(auth_token)
    sid = _launch_claude_via_webapp(base_url, auth_token)
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
