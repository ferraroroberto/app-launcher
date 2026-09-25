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

import functools
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib3
from pathlib import Path
from typing import Callable, IO, Iterator, List, Optional, Tuple

import psutil
import pytest
import requests
from playwright.sync_api import BrowserContext, Page, expect

from src.git_utils import run_git
from src.scanner import dir_ignored, slugify

from tests._credential_hygiene import (
    count_leaked_credentials,
    disposable_token,
    disposable_webapp_config,
    register_secret,
)
from tests.e2e._browser_sweep import sweep_browser_helpers
# The stub child + claude shim live in a plain module the synthetic design-review
# instance shares (app-launcher#1227); the private names stay for this file's callers.
from tests.e2e.stub_session import STUB_BANNER as _STUB_BANNER
from tests.e2e.stub_session import STUB_FLAG as _STUB_FLAG
from tests.e2e.stub_session import write_claude_shim as _write_claude_shim

logger = logging.getLogger(__name__)

# The webapp uses a self-signed cert; silence the urllib3 noise from /healthz.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WEBAPP_CONFIG = _REPO_ROOT / "config" / "webapp_config.json"
_SESSIONS_DIR = _REPO_ROOT / "webapp" / "sessions"
_BASE_URL = "https://127.0.0.1:8445"
_TOKEN_KEY = "launcher.token"  # must match TOKEN_KEY in app/webapp/static/state.js

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
# its own temp config, derived from the real one so it still boots with
# realistic values but carrying no credential (issue #907).
_WEBAPP_CONFIG_PATH_ENV = "LAUNCHER_WEBAPP_CONFIG"
# Env var the webapp honours to override the boot-autostart Startup directory
# (see src/boot_autostart.py:STARTUP_DIR_ENV) — the injection that stops the
# boot-autostart e2e test from ever reading/writing the real per-user Startup
# folder (issue #698). Without it, `/api/settings/boot-autostart` resolves
# the real folder, so the test could only pass on a host with no
# AppLauncher.bat installed there for real login-time autostart.
_STARTUP_DIR_ENV = "LAUNCHER_STARTUP_DIR"
# Env var both the webapp and the session-host honour to relocate the audit
# runtime dir (see src/audit.py:AUDIT_DIR_ENV) — the injection that stops a
# gate run from writing its throwaway `<sid>.log` / `<sid>.transcript` pairs
# into the checkout's live `webapp/sessions` (issue #913). Before it, a gate
# in the primary checkout wrote its whole run straight into the directory the
# user's phone reads — measured at 228 files (114 sessions) for one full
# dual-projection run — because src/audit.py resolves that path from
# `__file__`, so pointing the disposable processes at a temp *config* (#911)
# never moved their session files.
_AUDIT_DIR_ENV = "LAUNCHER_AUDIT_DIR"
# Env var the session-host honours to relocate the root uploaded files hang
# off (see app/session_host/server.py:UPLOAD_ROOT_ENV) — the same defect as
# #913 in a different directory (issue #922). The disposable session-host
# runs its sessions with `project_dir` set to this checkout, so every
# compose-bar upload test wrote a file into the checkout's own
# `.launcher-tmp`: 3,423 of the 3,600 files there were this suite's, with
# nothing pruning them. A file-count leak, not a disk-space one — they are
# 1x1 PNGs totalling ~15 KB, and the directory's bulk is real attachments.
# Only the session-host needs the variable — the webapp proxies
# `/sessions/{sid}/image` and never writes the file itself.
_UPLOAD_ROOT_ENV = "LAUNCHER_UPLOAD_ROOT"
_UPLOADS_DIR = _REPO_ROOT / ".launcher-tmp"
# Filename marker every harness upload carries, so the teardown breach check
# below accuses only on files this suite wrote. Same reasoning as the
# `[e2e-stub]` transcript banner: a real photo the user sends from the phone
# to the live tray mid-run must never trip the check.
_UPLOAD_MARKER = "e2e-stub-"
_AUTOBOOT_ENV = "LAUNCHER_E2E_AUTOBOOT"
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


def stable_read(read: Callable[[], object], attempts: int = 50,
                interval_s: float = 0.1) -> object:
    """Retry a raw DOM measurement past a mid-render stale element handle.

    Playwright's ``locator.evaluate()`` / ``bounding_box()`` resolve the
    selector to an element handle and *then* read from it. Any surface that
    rebuilds its DOM on a timer can invalidate that handle in between, and the
    read silently returns an artifact rather than raising: WebKit yields ``''``
    from ``getComputedStyle`` (shorthand *and* longhand) and ``None`` from
    ``bounding_box()``; ``scrollWidth``/``clientWidth`` both read ``0``.

    The Board is exactly such a surface — ``renderBoard()`` unconditionally
    calls ``list.replaceChildren()`` on every column, and ``fetchBoard()``
    re-renders every ``BOARD_POLL_MS`` (5 s) while the Board tab is up with no
    drawer open. So a board test that runs longer than 5 s *will* eventually
    read across a rebuild (#680: measured 3 bad reads in 700 with every fetch
    stubbed, at the 5 s cadence). Auto-retrying ``expect()`` assertions
    re-resolve and are immune; these raw reads are not.

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


# Parallel workers (#1231): under pytest-xdist every worker is its own pytest
# process booting its own disposable webapp + session-host, so what was one
# run's private state — a fixed log name, a port picked and then released —
# is now shared between workers. "" when the run is not distributed.
_XDIST_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "")
# How many fresh ports a disposable server gets before autoboot gives up. A
# retry only follows a lost port race, never a slow boot.
_BOOT_PORT_ATTEMPTS = 3


def _autoboot_log_name(stem: str) -> str:
    """``webapp/<stem>.log``, suffixed with the xdist worker id when there is
    one, so parallel workers never truncate each other's boot log."""
    return f"{stem}-{_XDIST_WORKER}.log" if _XDIST_WORKER else f"{stem}.log"


def _owns_listener(proc: subprocess.Popen, port: int) -> bool:
    """True when ``proc`` itself, or a descendant, listens on ``port``.

    A descendant counts because the venv's ``python.exe`` is a launcher stub
    that runs the real interpreter as its child. Anything else holding the
    port — another worker's server that won the race for it — does not.
    """
    try:
        family = {proc.pid} | {
            child.pid for child in psutil.Process(proc.pid).children(recursive=True)
        }
    except psutil.Error:
        return False
    return any(
        conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port
        and conn.pid in family
        for conn in psutil.net_connections(kind="tcp")
    )


def _boot_on_free_port(
    what: str,
    log_name: str,
    spawn: Callable[[int], subprocess.Popen],
    ready: Callable[[int], bool],
    timeout: float,
) -> Tuple[subprocess.Popen, int]:
    """Spawn ``what`` on a free port and wait until *it* answers there.

    ``_free_tcp_port`` releases the port before the child binds it, so another
    process can take it first — rare with one pytest process, a real race
    with several workers booting at once (#1231). ``ready`` alone cannot tell
    that apart from success: a port that listens, or a /healthz that answers,
    may be the other process. So readiness also requires the listener to be
    ours, and a lost race (the child exits, or someone else holds its port)
    retries on a fresh port. A child still alive at ``timeout`` with its port
    held by itself or by nobody is a slow or broken boot, not a race, and
    fails at once with its own message.
    """
    for attempt in range(1, _BOOT_PORT_ATTEMPTS + 1):
        port = _free_tcp_port()
        proc = spawn(port)
        deadline = time.time() + timeout
        while time.time() < deadline and proc.poll() is None:
            if ready(port) and _owns_listener(proc, port):
                return proc, port
            time.sleep(0.3)
        alive = proc.poll() is None
        if alive and (_owns_listener(proc, port) or not _port_listening(port)):
            _terminate(proc)
            pytest.fail(
                f"autoboot: {what} did not come up on :{port} within "
                f"{timeout:.0f}s — see webapp/{log_name}"
            )
        _terminate(proc)
        logger.warning(
            "⚠️ autoboot: %s lost the race for :%d (attempt %d/%d, %s) — retrying on a new port",
            what, port, attempt, _BOOT_PORT_ATTEMPTS,
            "another process holds it" if alive else "it exited",
        )
    pytest.fail(
        f"autoboot: {what} could not get a port of its own in {_BOOT_PORT_ATTEMPTS} "
        f"attempts — each time it exited or another process held the port; "
        f"see webapp/{log_name}"
    )


def _healthz_ok(base: str) -> bool:
    try:
        return requests.get(f"{base}/healthz", timeout=2, verify=False).status_code == 200
    except requests.RequestException:
        return False


def _leaked_stub_sessions(
    sessions_dir: Path, since: float, *, limit: int = 5
) -> List[Path]:
    """Harness-written session files that landed in the *real* sessions dir.

    A file counts only when it is newer than ``since`` **and** carries the
    `[e2e-stub]` banner, so a genuine session written by the live tray while
    the gate runs is never mistaken for a leak. Stops after ``limit`` hits:
    the result names the breach, it is not a census — the caller must not
    report its length as a total. Returns an empty list when the directory
    doesn't exist or can't be scanned; a scan that cannot run proves nothing,
    and this check only ever accuses on positive evidence.
    """
    hits: List[Path] = []
    try:
        with os.scandir(sessions_dir) as entries:
            for entry in entries:
                if not entry.name.endswith(".transcript"):
                    continue
                try:
                    if entry.stat().st_mtime < since:
                        continue
                    with open(
                        entry.path, "r", encoding="utf-8", errors="replace"
                    ) as fh:
                        head = fh.read(4096)
                except OSError:
                    continue
                if _STUB_BANNER in head:
                    hits.append(Path(entry.path))
                    if len(hits) >= limit:
                        break
    except OSError:
        return hits
    return hits


def _leaked_stub_uploads(
    uploads_dir: Path, since: float, *, limit: int = 5
) -> List[Path]:
    """Harness-written uploads that landed in the *real* `.launcher-tmp`.

    Marker-based like :func:`_leaked_stub_sessions`, not a plain before/after
    count: the live tray shares this checkout, so a photo the user attaches
    from the phone while the gate runs writes here legitimately. A file counts
    only when it is newer than ``since`` **and** its name carries
    ``_UPLOAD_MARKER``, which only this suite's uploads do. Stops after
    ``limit`` hits — the result names the breach, it is not a census. Returns
    an empty list when the directory doesn't exist or can't be scanned: a scan
    that cannot run proves nothing, and this check only accuses on positive
    evidence.
    """
    hits: List[Path] = []
    try:
        with os.scandir(uploads_dir) as entries:
            for entry in entries:
                if _UPLOAD_MARKER not in entry.name:
                    continue
                try:
                    if entry.stat().st_mtime < since:
                        continue
                except OSError:
                    continue
                hits.append(Path(entry.path))
                if len(hits) >= limit:
                    break
    except OSError:
        return hits
    return hits


@pytest.fixture(scope="session")
def _autoboot_server(
    tmp_path_factory: pytest.TempPathFactory, auth_token: str
) -> Iterator[str]:
    """Spawn a disposable webapp (+ session-host) and yield its base URL.

    A hard failure (`pytest.fail`) — never a skip — if anything doesn't come
    up: under the pre-ship gate a missing server must not pass silently.
    """
    from app.webapp.event_loop import LOOP_FACTORY
    from app.webapp.manager import cert_paths
    from src import boot_autostart

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

    # Config isolation (issue #441): the disposable webapp gets its own temp
    # config — realistic values (projects_dir, agent settings, …) without
    # write access to the real file. Any e2e test that Saves settings mutates
    # only this one. Snapshot the real file's bytes so the isolation can be
    # *asserted* after the run, not just assumed.
    #
    # Credential isolation (issue #907): it is derived from the real config
    # with every credential dropped and this run's disposable auth_token in
    # their place — the live token must never sit in a test run's config —
    # and it lives in pytest's temp tree, not the checkout. Loopback bypasses
    # the bearer gate, so nothing here needs the real one.
    cfg_copy = tmp_path_factory.mktemp("webapp-config") / "webapp_config.json"
    real_cfg_bytes = (
        _WEBAPP_CONFIG.read_bytes() if _WEBAPP_CONFIG.exists() else None
    )
    disposable_cfg = disposable_webapp_config(_WEBAPP_CONFIG, auth_token)
    pin_launch_target(disposable_cfg, launch_target_dir() or _REPO_ROOT)
    if count_leaked_credentials(_WEBAPP_CONFIG, disposable_cfg):
        pytest.fail(
            "autoboot: the disposable webapp config still holds a credential "
            f"from {_WEBAPP_CONFIG} (issue #907) — every key in "
            "src.webapp_config.CREDENTIAL_KEYS must be dropped. Value withheld."
        )
    cfg_copy.write_text(json.dumps(disposable_cfg, indent=2), encoding="utf-8")

    # Startup-folder isolation (issue #698): give the disposable webapp its
    # own temp Startup dir so `src.boot_autostart.enable()/disable()` (called
    # with no override by `/api/settings/boot-autostart`) never touches the
    # real per-user Startup folder. That lets the boot-autostart e2e test
    # assume it starts OFF regardless of whether this host has
    # AppLauncher.bat installed for real login-time autostart. Snapshot the
    # real wrapper bat (existence + bytes) so the isolation can be *asserted*
    # after the run, not just assumed — mirrors the webapp-config check below.
    startup_dir = tmp_path_factory.mktemp("startup-dir")
    real_wrapper_bat = boot_autostart.wrapper_bat_path()
    real_wrapper_bytes = (
        real_wrapper_bat.read_bytes() if real_wrapper_bat.is_file() else None
    )

    # Session-file isolation (issue #913): src/audit.py resolves its directory
    # from `__file__`, so both disposable processes — spawned from this very
    # checkout — appended their throwaway session logs and transcripts to the
    # live `webapp/sessions`. Measured on this box: 94,672 files, ~400/day,
    # the overwhelming majority `[e2e-stub]` sessions from gate runs. Give
    # them a per-run temp dir; publish it so the log poller below reads the
    # same place, and snapshot the run start so the teardown can assert no
    # harness session leaked into the real directory.
    audit_dir = tmp_path_factory.mktemp("audit-dir")
    _AUTOBOOT_STATE["sessions_dir"] = audit_dir / "sessions"

    # Upload isolation (issue #922): same defect, different directory. The
    # disposable session-host runs its sessions with `project_dir` pointing at
    # this checkout, so `_save_image` resolved `<checkout>/.launcher-tmp` and
    # every compose-bar attach test left its file there — 3,423 of the 3,600
    # files in it before the fix. Give it a per-run temp root; the teardown
    # asserts nothing marked as ours reached the real directory.
    uploads_root = tmp_path_factory.mktemp("upload-root")

    run_started_at = time.time()

    try:
        # Session-host: ALWAYS spawn our own on a free port — never adopt a
        # host already listening on the live :8446, which on a dev box owns
        # the user's real PTY/Claude sessions (issue #260). A free, disposable
        # host starts empty, so the destructive e2e tests can only ever touch
        # sessions this run launched. The disposable webapp is pointed at it
        # via LAUNCHER_SESSION_HOST_PORT below.
        # Lightweight-child shim (issue #534): only the DISPOSABLE
        # session-host gets the shim on PATH — the pytest process and the
        # live tray keep the real resolution, so `shutil.which("claude")`
        # in the fixtures below still faithfully predicts the real CLI.
        shim_dir = tmp_path_factory.mktemp("claude-shim")
        _write_claude_shim(shim_dir)
        sh_log_name = _autoboot_log_name("e2e-autoboot-session-host")
        sh_log = _open_log(sh_log_name)

        def _spawn_session_host(port: int) -> subprocess.Popen:
            return _spawn(
                [sys.executable, str(_REPO_ROOT / "launcher.py"),
                 "session-host", "--port", str(port)],
                sh_log,
                extra_env={
                    "PATH": f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                    _AUDIT_DIR_ENV: str(audit_dir),
                    _UPLOAD_ROOT_ENV: str(uploads_root),
                },
            )

        sh_proc, sh_port = _boot_on_free_port(
            "session-host", sh_log_name, _spawn_session_host, _port_listening,
            timeout=15,
        )
        _AUTOBOOT_STATE["session_host_port"] = sh_port

        # Webapp on a free port. HTTPS when the cert pair exists (mirrors the
        # real phone path); plain HTTP otherwise so a cert-less checkout still
        # runs the gate.
        certs = cert_paths()
        scheme = "https" if certs else "http"
        wa_log_name = _autoboot_log_name("e2e-autoboot-webapp")
        wa_log = _open_log(wa_log_name)

        def _spawn_webapp(port: int) -> subprocess.Popen:
            wa_cmd = [
                sys.executable, "-m", "uvicorn", "app.webapp.server:app",
                "--host", "127.0.0.1", "--port", str(port),
                "--log-level", "warning", "--loop", LOOP_FACTORY,
            ]
            if certs:
                cert, key = certs
                wa_cmd += ["--ssl-keyfile", str(key), "--ssl-certfile", str(cert)]
            # Point the disposable webapp at our disposable session-host, not
            # the config's :8446, and at the temp config copy, not the real
            # file — the two env injections that isolate the gate (issues
            # #260, #441).
            return _spawn(
                wa_cmd,
                wa_log,
                extra_env={
                    _SESSION_HOST_PORT_ENV: str(sh_port),
                    _WEBAPP_CONFIG_PATH_ENV: str(cfg_copy),
                    _STARTUP_DIR_ENV: str(startup_dir),
                    _AUDIT_DIR_ENV: str(audit_dir),
                },
            )

        wa_proc, port = _boot_on_free_port(
            "webapp", wa_log_name, _spawn_webapp,
            lambda p: _healthz_ok(f"{scheme}://127.0.0.1:{p}"), timeout=20,
        )
        base = f"{scheme}://127.0.0.1:{port}"
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
        # Startup-folder isolation regression check (issue #698): the real
        # wrapper bat must be byte-identical to the pre-run snapshot. A
        # mismatch means some path wrote to the real Startup folder during
        # the gate instead of the temp startup_dir — the owner boots the
        # launcher from this file.
        current_wrapper_bytes = (
            real_wrapper_bat.read_bytes() if real_wrapper_bat.is_file() else None
        )
        if current_wrapper_bytes != real_wrapper_bytes:
            raise RuntimeError(
                f"e2e autoboot isolation breach: {real_wrapper_bat} changed "
                "during the run. The disposable webapp must only ever write "
                f"the temp Startup dir ({startup_dir}) — a test wrote to the "
                "real Startup folder instead. Fix that before shipping."
            )
        # Session-file isolation regression check (issue #913): no session
        # this harness created may have landed in the checkout's real
        # `webapp/sessions`. Keyed on the `[e2e-stub]` banner the lightweight
        # PTY child prints (#534), which only this harness can produce — so a
        # real session written by the live tray during the gate can never
        # trip it, and the check needs no exclusive access to the directory.
        leaked = _leaked_stub_sessions(_SESSIONS_DIR, run_started_at)
        if leaked:
            raise RuntimeError(
                f"e2e autoboot isolation breach: at least {len(leaked)} harness "
                f"session file(s) landed in {_SESSIONS_DIR} during the run "
                f"instead of the temp audit dir ({audit_dir}) — e.g. "
                f"{leaked[0].name}. Every process this fixture spawns must get "
                f"{_AUDIT_DIR_ENV} (issue #913)."
            )
        # Upload isolation regression check (issue #922): no file this harness
        # uploaded may have landed in the checkout's real `.launcher-tmp`.
        # Keyed on the `_UPLOAD_MARKER` filename prefix every harness upload
        # carries, so a photo the user attaches on the live tray during the
        # gate can never trip it.
        leaked_uploads = _leaked_stub_uploads(_UPLOADS_DIR, run_started_at)
        if leaked_uploads:
            raise RuntimeError(
                f"e2e autoboot isolation breach: at least {len(leaked_uploads)} "
                f"harness upload(s) landed in {_UPLOADS_DIR} during the run "
                f"instead of the temp upload root ({uploads_root}) — e.g. "
                f"{leaked_uploads[0].name}. The disposable session-host must "
                f"get {_UPLOAD_ROOT_ENV} (issue #922)."
            )


@pytest.fixture(scope="session")
def base_url(request: pytest.FixtureRequest) -> str:
    if _autoboot_enabled(request.config):
        return request.getfixturevalue("_autoboot_server")
    return _BASE_URL


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
    result = sweep_browser_helpers(_REPO_ROOT)
    print(f"\n{result.summary()}")
    for entry in result.killed:
        print(f"  reclaimed leaked helper: {entry}")


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
def chromium_projection_only(browser_name: str) -> None:
    """Skip the WebKit projection for a check with no browser-dependent signal.

    Opt in per module with ``pytest.mark.usefixtures("chromium_projection_only")``:
    server-side ``requests`` checks, and pure-JS helpers probed through
    ``page.evaluate`` with no DOM geometry or CSS, give the same answer on both
    engines, so a second projection only doubles the runtime (#954). Chromium is
    the one kept because the diff-proportionate gate's narrow static tier
    (#568) runs Chromium only. Session-scoped so the skip fires before any
    function-scoped fixture (browser context, PTY launch) is built for nothing.
    """
    if browser_name != "chromium":
        pytest.skip("no browser-dependent signal; runs once on the chromium projection")


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
# as a black box — #186's point, which a blanket rise in _DEFAULT_TIMEOUT_MS
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


# --------------------------------------------------- opening a session row
#
# #1025 removed the running-sessions row's actions gear, which used to be the
# one-tap way into a specific mode (its menu held Terminal / Chat / Rename /
# Stop). The row is now a single tappable button, so a test that wants a mode
# or a session action goes in through the overlay the tap opens. These two
# helpers are that path, shared so the ~7 files that used the gear don't each
# grow their own version.


def stub_session_mirror(page: Page) -> None:
    """Answer the PC-mirror POST with ``mirrored: false``.

    On a **desktop** browser (``pointer: fine`` — the Chromium projection,
    never the iPhone one) a tap on a *full-control* row opens a dedicated PC
    Edge window instead of the in-page overlay (#282). ``mirrored: false`` is
    the server's own "mirroring is disabled" answer and ``openSession``'s
    supported fall-through to the in-page overlay, so stubbing it is what lets
    one row-tap test run on both projections rather than skipping Chromium.

    A test whose subject *is* the mirror must not use this — see
    ``test_desktop_session_mirror.py``.
    """
    page.route(
        re.compile(r".*/api/claude-code/sessions/[^/]+/mirror$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"mirrored": false}'
        ),
    )


def open_session_row(page: Page, row, mode: str | None = None) -> None:
    """Tap a running-sessions row and leave the overlay open in ``mode``.

    ``mode`` (``"terminal"`` / ``"chat"``) taps the bar's segmented toggle
    after the overlay opens; ``None`` accepts whatever mode the row opened in
    (a full-control row's last-viewed, a detached one's Chat). Call
    ``stub_session_mirror`` first when the row is full-control and the test
    runs on the Chromium projection.

    Uses the pool-wide ``OVERLAY_OPEN_MS`` budget (#887), never a literal.
    """
    row.locator(".session-open").click()
    page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)
    if mode is not None:
        seg = "#sessionModeChat" if mode == "chat" else "#sessionModeTerminal"
        page.locator(seg).click()
        expect(page.locator("#terminalOverlay")).to_have_attribute("data-mode", mode)


def session_menu_item(page: Page, name: str):
    """Open the overlay bar's ⋮ menu and return the item with ``aria-label``
    ``name`` (``Rename session`` / ``Copy session link`` /
    ``Stop and kill session``). The overlay must already be open."""
    page.locator("#terminalMenu").click()
    expect(page.locator("#terminalOverlay .terminal-menu")).to_be_visible()
    return page.get_by_role("menuitem", name=name)


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
# these; other tests don't pay any launch + teardown cost. The lightweight stub
# session runs in THIS checkout; the real-agent session runs in the first
# agent-trusted checkout of this repository (see `launch_target_dir`).
# Self-launching is harmless — it just spawns the agent in a repo dir, and the
# real-agent pin never submits a prompt.
#
# Neither is the literal `app-launcher` any more (issue #932). A
# merge-verification run works from a fresh detached checkout under a scratch
# root (`E:\tmp\al-merge-<date>`), where no sibling directory is called
# `app-launcher` at all — so the hardcoded id resolved to no coding row, the
# launch 404'd, and the real-agent tests *skipped* while the gate printed the
# same green as a run that covered them.
_CHECKOUT_ID = slugify(_REPO_ROOT.name)

# Claude Code's per-directory folder-trust gate (issue #932). A directory the
# user has never opened the agent in gets a full-screen "Is this a project you
# created or one you trust?" prompt on first launch, *instead* of the composer
# — so a real-agent assertion can never land there. It is independent of the
# permission mode: `--dangerously-skip-permissions` was measured NOT to clear
# it. The only ways to clear it are answering the prompt or writing
# `hasTrustDialogAccepted` into the user's global `~/.claude.json`, and a test
# harness must do neither — so a real-agent test launches in a checkout that
# already cleared it, or skips naming trust rather than time out against a
# prompt that will never go away.
_CLAUDE_STATE_FILE = Path.home() / ".claude.json"


def agent_trusts_dir(project_dir: Path) -> Optional[bool]:
    """Has the agent's folder-trust gate been accepted for ``project_dir``?

    ``None`` when the answer can't be established at all (no state file, or
    one this harness can't parse) — an unknown, never folded into either
    answer. A directory absent from the map is a definite ``False``: the agent
    records an entry when the gate is accepted.
    """
    try:
        state = json.loads(_CLAUDE_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    projects = state.get("projects") if isinstance(state, dict) else None
    if not isinstance(projects, dict):
        return None

    def _key(value: str) -> str:
        return os.path.normcase(os.path.normpath(value))

    want = _key(str(project_dir))
    for recorded, entry in projects.items():
        if _key(str(recorded)) == want:
            return bool(
                isinstance(entry, dict) and entry.get("hasTrustDialogAccepted")
            )
    return False


def repository_checkouts(repo_root: Path) -> List[Path]:
    """``repo_root``, then its repository's main checkout if it is a linked worktree.

    A linked worktree (`app-launcher-wt-N`) has never cleared the agent's
    trust gate, but the main checkout it hangs off usually has — and before
    #932 the literal target id quietly launched there, so worktree gates ran
    the real-agent pin. Keeping that as an explicit, derived fallback stops
    the fix for fresh checkouts from costing worktrees the same coverage. A
    fresh clone *is* its own main checkout, so it gets no fallback.
    """
    checkouts = [repo_root]
    common = run_git(
        repo_root, ["rev-parse", "--path-format=absolute", "--git-common-dir"]
    )
    if common:
        common_dir = Path(common)
        if common_dir.name == ".git":
            main = common_dir.parent
            if os.path.normcase(str(main.resolve())) != os.path.normcase(
                str(repo_root.resolve())
            ):
                checkouts.append(main)
    return checkouts


_TrustProbe = Tuple[Tuple[Path, Optional[bool]], ...]


@functools.lru_cache(maxsize=None)
def _launch_target_resolution() -> Tuple[Optional[Path], _TrustProbe]:
    """Resolve once per run: (first trusted checkout or ``None``, every probe)."""
    probed = tuple((d, agent_trusts_dir(d)) for d in repository_checkouts(_REPO_ROOT))
    target = next((d for d, trusted in probed if trusted is True), None)
    return target, probed


def launch_target_dir() -> Optional[Path]:
    """The checkout a real-agent test launches in, or ``None`` if none is trusted."""
    return _launch_target_resolution()[0]


def pin_launch_target(cfg: dict, target: Path) -> dict:
    """Make ``cfg`` scan ``target``, so its coding row always resolves.

    Pins the Coding-tab scan root to ``target``'s parent and drops any
    inherited ignore pattern that would hide ``target`` itself. On the primary
    checkout this is already exactly what the real config says, so it changes
    nothing there; in a fresh scratch checkout (which has no config at all) it
    is what stops the launch 404ing (issue #932). Nothing here is a credential
    — a checkout's own path is not secret — so it does not reopen #907 / PR
    #911.

    Mutates and returns ``cfg``. Pinned by ``tests/test_e2e_launch_target.py``.
    """
    cfg["projects_dir"] = str(target.parent)
    cfg["projects_ignore"] = [
        pattern
        for pattern in (cfg.get("projects_ignore") or [])
        if not dir_ignored(target.name, [pattern])
    ]
    return cfg


def _require_trusted_launch_target() -> Path:
    """The real-agent launch target, or a skip that names why there is none."""
    target, probed = _launch_target_resolution()
    if target is not None:
        return target
    states = "; ".join(
        f"{d}: {'untrusted' if trusted is False else 'UNKNOWN'}"
        for d, trusted in probed
    )
    pytest.skip(
        "no checkout of this repository has cleared the agent's folder-trust "
        f"gate ({states}), so the agent would paint its trust prompt instead "
        "of a composer and a real-agent assertion can never land (issue #932). "
        "An unknown state is not assumed trusted. Clearing the gate would mean "
        "writing the user's global agent state, which the gate must not do — "
        "this is the residual coverage a fresh detached checkout costs."
    )


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


def _launch_claude_via_webapp(
    base_url: str, auth_token: str, *, autoboot: bool = False
) -> str:
    """Launch a REAL claude PTY session through the webapp's launch endpoint.

    Where `claude` isn't on PATH — notably the CI runner, which never
    installs it — the PTY child exits at once ("'claude' is not
    recognized…"), the session-host reaps it, and its WS endpoint then 403s
    the webapp's proxy, so every consumer would race a corpse. Skip cleanly
    instead: these tests genuinely gate on a dev box where `claude` runs.
    The test process shares the live session-host's PATH (same machine), so
    `which` here faithfully predicts whether the session-host can spawn it.
    See #58.

    The target is the first agent-trusted checkout of this repository — a
    skip naming trust when there is none (issue #932).
    """
    if shutil.which("claude") is None:
        pytest.skip(
            "`claude` is not on PATH — real-Claude PTY tests need a live "
            "claude CLI and skip cleanly where it isn't installed (e.g. the "
            "CI runner)"
        )
    target_id = slugify(_require_trusted_launch_target().name)

    headers = _auth_headers(auth_token)
    try:
        res = requests.post(
            f"{base_url}/api/apps/{target_id}/launch",
            # The test renders the session in its own Playwright page, so say
            # so: without `in_page` the launch mirrors to a real PC Edge window
            # (issue #938; the disposable instance also refuses to mirror).
            json={"mode": "pty", "in_page": True},
            headers=headers,
            verify=False,
            timeout=10,
        )
    except Exception as exc:
        pytest.skip(f"launch request failed: {exc.__class__.__name__}: {exc}")

    if res.status_code != 200:
        detail = f"HTTP {res.status_code}: {res.text[:200]}"
        if autoboot:
            # Under the gate the disposable config pins the scan root to the
            # launch target's parent, so the row always exists — a non-200
            # here is a harness bug, never a missing dependency. It used to
            # skip, which is how #932's fresh-checkout 404 ("unknown app
            # app-launcher") cost two tests while the gate still printed green.
            pytest.fail(
                f"could not launch PTY session for target {target_id!r} "
                f"({detail}) — under autoboot this is a harness bug (issue #932)"
            )
        # 400 is the expected failure when the project_dir is invalid — skip
        # cleanly rather than fail the suite.
        pytest.skip(f"could not launch PTY session ({detail})")

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
                "name": _CHECKOUT_ID,
                "flags": _STUB_FLAG,
                "agent": "claude",
            },
            # The listener is already health-checked; the response waits for
            # ConPTY creation, measured at 19 s on this host under gate load.
            timeout=(5, 30),
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
def launched_claude_pty_session(
    request: pytest.FixtureRequest, base_url: str, auth_token: str
) -> Iterator[str]:
    """A live PTY session running the REAL Claude CLI (issue #534).

    Only for tests whose assertions depend on the real agent — rendered
    Claude output in the xterm buffer, agent input echo, Claude lifecycle
    semantics. Spawns a real node process per test: keep its consumer set
    minimal, and put UI-only assertions on `launched_pty_session`.

    Precondition: some checkout of this repository must already have cleared
    the agent's folder-trust gate (issue #932) — a fresh detached clone has
    not, and the agent paints its trust prompt where the composer should be.
    """
    headers = _auth_headers(auth_token)
    sid = _launch_claude_via_webapp(
        base_url, auth_token, autoboot=_autoboot_enabled(request.config)
    )
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
    ``<sessions dir>/<sid>.log`` every 200 ms until ``needle`` appears or the
    deadline elapses, then returns ``True``/``False``. The directory is the
    autoboot temp dir when the gate redirected it (issue #913), else the
    checkout's own ``webapp/sessions`` — live mode reads the real one. One
    source of truth for the input-delivery wait that used to be a hardcoded
    5 s poll loop copied into four test files (issue #58).
    """

    def _wait(
        page: Page,
        sid: str,
        needle: str,
        deadline_ms: int = _LOG_POLL_DEADLINE_MS,
    ) -> bool:
        sessions_dir = _AUTOBOOT_STATE.get("sessions_dir", _SESSIONS_DIR)
        log_path = sessions_dir / f"{sid}.log"

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


@pytest.fixture
def session_log_path() -> Callable[[str], Path]:
    """Return ``sid -> <sessions dir>/<sid>.log``.

    Same directory resolution as ``wait_for_session_log`` — autoboot temp dir
    when the gate redirected it (issue #913), else the checkout's own
    ``webapp/sessions`` — exposed for the assertions that must *count* logged
    chunks rather than wait for one to appear (issue #1024: one Ctrl+C tap
    delivers exactly one ``\\x03``, never two).
    """

    def _path(sid: str) -> Path:
        sessions_dir = _AUTOBOOT_STATE.get("sessions_dir", _SESSIONS_DIR)
        return sessions_dir / f"{sid}.log"

    return _path
