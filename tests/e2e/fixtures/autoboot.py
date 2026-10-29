"""Boot of the disposable webapp + session-host the pre-ship gate runs against.

``tests/e2e/conftest.py``'s ``_autoboot_server`` fixture is the entry point; the
steps live here so each stays a short function: ``prepare_isolation`` gives the
run its own temp config, Startup, audit, upload and runtime-data folders,
``boot_session_host`` and ``boot_webapp`` start the two servers on free ports,
and ``finish_run`` reaps stub children and asserts nothing reached the real
checkout.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Iterator, List, Optional

import pytest

from src.clients.github import GH_CMD_ENV
from src.desktop.input import FAKE_INPUT_ENV as _FAKE_INPUT_ENV

from tests._credential_hygiene import count_leaked_credentials, disposable_webapp_config
from tests.e2e._autoboot_env import runtime_data_env
from tests.e2e._process_teardown import reap_stub_children
from tests.e2e._process_teardown import terminate_tree as _terminate
from tests.e2e.fixtures.env import (
    AUDIT_DIR_ENV,
    AUTOBOOT_STATE,
    REPO_ROOT,
    SESSION_HOST_PORT_ENV,
    SESSIONS_DIR,
    STARTUP_DIR_ENV,
    SYNTHETIC_GH_CMD,
    UPLOAD_ROOT_ENV,
    UPLOADS_DIR,
    WEBAPP_CONFIG,
    WEBAPP_CONFIG_PATH_ENV,
)
from tests.e2e.fixtures.launch_target import launch_target_dir, pin_launch_target, pin_ui_prefs
from tests.e2e.fixtures.processes import (
    autoboot_log_name,
    boot_on_free_port,
    healthz_ok,
    leaked_stub_sessions,
    leaked_stub_uploads,
    port_listening,
    spawn,
)
from tests.e2e.stub_session import write_claude_shim as _write_claude_shim

logger = logging.getLogger(__name__)


@dataclass
class Isolation:
    """The per-run folders and real-file snapshots the teardown asserts against."""

    cfg_copy: Path
    real_cfg_bytes: Optional[bytes]
    startup_dir: Path
    real_wrapper_bat: Path
    real_wrapper_bytes: Optional[bytes]
    audit_dir: Path
    uploads_root: Path
    runtime_data_dir: Path
    shim_dir: Path
    run_started_at: float


@dataclass
class Servers:
    """The two disposable processes and the log handles they write to."""

    logs_dir: Path
    handles: List[IO[str]] = field(default_factory=list)
    sh_proc: Optional[subprocess.Popen] = None
    wa_proc: Optional[subprocess.Popen] = None

    def open_log(self, name: str) -> IO[str]:
        handle = (self.logs_dir / name).open("w", encoding="utf-8", errors="replace")
        self.handles.append(handle)
        return handle

    def stop(self) -> None:
        _terminate(self.wa_proc)
        if self.sh_proc is not None:  # always ours now (never an adopted tray)
            _terminate(self.sh_proc)
        for handle in self.handles:
            try:
                handle.close()
            except Exception:  # pragma: no cover
                pass


def prepare_isolation(tmp_path_factory: pytest.TempPathFactory, auth_token: str) -> Isolation:
    """Give the run private config, Startup, audit, upload and runtime-data folders."""
    from src import boot_autostart

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
    real_cfg_bytes = WEBAPP_CONFIG.read_bytes() if WEBAPP_CONFIG.exists() else None
    #
    # Host-state isolation (issue #1717): the hook-state files the Board and
    # usage meter read (rate limits, sessions, chief plan/inbox, context
    # filter) are pinned into a per-run temp folder, so a capture shows
    # synthetic usage rather than the machine's real percentages.
    host_state_dir = tmp_path_factory.mktemp("host-state")
    disposable_cfg = disposable_webapp_config(WEBAPP_CONFIG, auth_token, host_state_dir)
    pin_launch_target(disposable_cfg, launch_target_dir() or REPO_ROOT)
    pin_ui_prefs(disposable_cfg)
    if count_leaked_credentials(WEBAPP_CONFIG, disposable_cfg):
        pytest.fail(
            "autoboot: the disposable webapp config still holds a credential "
            f"from {WEBAPP_CONFIG} (issue #907) — every key in "
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
    real_wrapper_bytes = real_wrapper_bat.read_bytes() if real_wrapper_bat.is_file() else None

    # Session-file isolation (issue #913): src/audit.py resolves its directory
    # from `__file__`, so both disposable processes — spawned from this very
    # checkout — appended their throwaway session logs and transcripts to the
    # live `webapp/sessions`. Measured on this box: 94,672 files, ~400/day,
    # the overwhelming majority `[e2e-stub]` sessions from gate runs. Give
    # them a per-run temp dir; publish it so the log poller reads the same
    # place, and snapshot the run start so the teardown can assert no harness
    # session leaked into the real directory.
    audit_dir = tmp_path_factory.mktemp("audit-dir")
    AUTOBOOT_STATE["sessions_dir"] = audit_dir / "sessions"

    # Upload isolation (issue #922): same defect, different directory. The
    # disposable session-host runs its sessions with `project_dir` pointing at
    # this checkout, so `_save_image` resolved `<checkout>/.launcher-tmp` and
    # every compose-bar attach test left its file there — 3,423 of the 3,600
    # files in it before the fix. Give it a per-run temp root; the teardown
    # asserts nothing marked as ours reached the real directory.
    uploads_root = tmp_path_factory.mktemp("upload-root")

    # Runtime-data isolation (issue #1685): the webapp mounts the usage log on
    # `runtime_db_path("app-launcher", ...)`, which with no override resolves
    # the real runtime-data folder, so every gate run wrote its browser
    # traffic (both projections, in bursts) into the file the owner's own
    # visits land in. A per-run temp folder keeps it, and every other runtime
    # file under that app directory, out of the real store. Webapp only: the
    # session-host's import closure never reads runtime data.
    runtime_data_dir = tmp_path_factory.mktemp("runtime-data")

    # Lightweight-child shim (issue #534): only the DISPOSABLE session-host
    # gets the shim on PATH — the pytest process and the live tray keep the
    # real resolution, so `shutil.which("claude")` in the fixtures still
    # faithfully predicts the real CLI. Made before the servers start because
    # the teardown reaps stub children by this directory (#1335).
    shim_dir = tmp_path_factory.mktemp("claude-shim")

    return Isolation(
        cfg_copy=cfg_copy,
        real_cfg_bytes=real_cfg_bytes,
        startup_dir=startup_dir,
        real_wrapper_bat=real_wrapper_bat,
        real_wrapper_bytes=real_wrapper_bytes,
        audit_dir=audit_dir,
        uploads_root=uploads_root,
        runtime_data_dir=runtime_data_dir,
        shim_dir=shim_dir,
        run_started_at=time.time(),
    )


def boot_session_host(iso: Isolation, servers: Servers) -> int:
    """Spawn our own session-host on a free port and return that port.

    ALWAYS our own — never adopt a host already listening on the live :8446,
    which on a dev box owns the user's real PTY/Claude sessions (issue #260).
    A free, disposable host starts empty, so the destructive e2e tests can
    only ever touch sessions this run launched.
    """
    _write_claude_shim(iso.shim_dir)
    sh_log_name = autoboot_log_name("e2e-autoboot-session-host")
    sh_log = servers.open_log(sh_log_name)

    def _spawn_session_host(port: int) -> subprocess.Popen:
        return spawn(
            [sys.executable, str(REPO_ROOT / "launcher.py"),
             "session-host", "--port", str(port)],
            sh_log,
            extra_env={
                "PATH": f"{iso.shim_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                AUDIT_DIR_ENV: str(iso.audit_dir),
                UPLOAD_ROOT_ENV: str(iso.uploads_root),
            },
        )

    servers.sh_proc, sh_port = boot_on_free_port(
        "session-host", sh_log_name, _spawn_session_host, port_listening, timeout=15,
    )
    AUTOBOOT_STATE["session_host_port"] = sh_port
    return sh_port


def boot_webapp(iso: Isolation, servers: Servers, sh_port: int) -> str:
    """Spawn the disposable webapp pointed at our session-host; return its base URL.

    HTTPS when the cert pair exists (mirrors the real phone path); plain HTTP
    otherwise so a cert-less checkout still runs the gate.
    """
    from app.webapp.manager import cert_paths

    certs = cert_paths()
    scheme = "https" if certs else "http"
    wa_log_name = autoboot_log_name("e2e-autoboot-webapp")
    wa_log = servers.open_log(wa_log_name)

    def _spawn_webapp(port: int) -> subprocess.Popen:
        wa_cmd = [
            sys.executable, "-m", "app.webapp.serve",
            "--host", "127.0.0.1", "--port", str(port),
            "--log-level", "warning",
        ]
        if certs:
            cert, key = certs
            wa_cmd += ["--ssl-keyfile", str(key), "--ssl-certfile", str(cert)]
        # Point the disposable webapp at our disposable session-host, not
        # the config's :8446, and at the temp config copy, not the real
        # file — the two env injections that isolate the gate (issues
        # #260, #441).
        return spawn(
            wa_cmd,
            wa_log,
            extra_env={
                SESSION_HOST_PORT_ENV: str(sh_port),
                WEBAPP_CONFIG_PATH_ENV: str(iso.cfg_copy),
                STARTUP_DIR_ENV: str(iso.startup_dir),
                AUDIT_DIR_ENV: str(iso.audit_dir),
                **runtime_data_env(iso.runtime_data_dir),
                GH_CMD_ENV: SYNTHETIC_GH_CMD,
                # The Screen view's input is recorded, never sent: the
                # gate runs on the owner's desktop (#1521).
                _FAKE_INPUT_ENV: "1",
            },
        )

    servers.wa_proc, port = boot_on_free_port(
        "webapp", wa_log_name, _spawn_webapp,
        lambda p: healthz_ok(f"{scheme}://127.0.0.1:{p}"), timeout=20,
    )
    return f"{scheme}://127.0.0.1:{port}"


def run_autoboot(
    tmp_path_factory: pytest.TempPathFactory, auth_token: str
) -> Iterator[str]:
    """Boot both servers, yield the webapp's base URL, then tear down and check isolation."""
    logs_dir = REPO_ROOT / "webapp"  # gitignored runtime dir
    logs_dir.mkdir(parents=True, exist_ok=True)
    servers = Servers(logs_dir)
    iso = prepare_isolation(tmp_path_factory, auth_token)
    try:
        sh_port = boot_session_host(iso, servers)
        base = boot_webapp(iso, servers, sh_port)
        logger.info("✅ autoboot: webapp ready at %s", base)
        yield base
    finally:
        servers.stop()
        finish_run(iso)


def finish_run(iso: Isolation) -> None:
    """Reap leaked stub children, then raise if anything reached the real checkout."""
    # Stub-child reap (issue #1335): a stub this run launched that is
    # still alive once both servers are down escaped the tree-aware stop,
    # and it pins the checkout until reboot. Reap it first, before any
    # check below can raise, and name it in the gate output so the path
    # it escaped by can be found. A stub the reap cannot kill raises.
    for leaked_stub in reap_stub_children(iso.shim_dir):
        print(f"\n⚠️ autoboot: reaped a leaked e2e stub child: {leaked_stub}")
    _assert_config_untouched(iso)
    _assert_startup_untouched(iso)
    _assert_no_leaked_sessions(iso)
    _assert_no_leaked_uploads(iso)


def _assert_config_untouched(iso: Isolation) -> None:
    # Isolation regression check (issue #441): the real config must be
    # byte-identical to the pre-run snapshot. A mismatch means some path
    # wrote to the real file during the gate — the exact class of bug
    # that corrupted session_host_port in #438 — or, rarely, that the
    # user saved settings on the LIVE tray mid-run. Loud either way.
    current = WEBAPP_CONFIG.read_bytes() if WEBAPP_CONFIG.exists() else None
    if current != iso.real_cfg_bytes:
        raise RuntimeError(
            f"e2e autoboot isolation breach: {WEBAPP_CONFIG} changed "
            "during the run. The disposable webapp must only ever write "
            f"its temp copy ({iso.cfg_copy}). If you changed settings on the "
            "live tray while the gate ran, rerun the gate; otherwise a "
            "test wrote to the real config — fix that before shipping."
        )


def _assert_startup_untouched(iso: Isolation) -> None:
    # Startup-folder isolation regression check (issue #698): the real
    # wrapper bat must be byte-identical to the pre-run snapshot. A
    # mismatch means some path wrote to the real Startup folder during
    # the gate instead of the temp startup_dir — the owner boots the
    # launcher from this file.
    current_wrapper_bytes = (
        iso.real_wrapper_bat.read_bytes() if iso.real_wrapper_bat.is_file() else None
    )
    if current_wrapper_bytes != iso.real_wrapper_bytes:
        raise RuntimeError(
            f"e2e autoboot isolation breach: {iso.real_wrapper_bat} changed "
            "during the run. The disposable webapp must only ever write "
            f"the temp Startup dir ({iso.startup_dir}) — a test wrote to the "
            "real Startup folder instead. Fix that before shipping."
        )


def _assert_no_leaked_sessions(iso: Isolation) -> None:
    # Session-file isolation regression check (issue #913): no session
    # this harness created may have landed in the checkout's real
    # `webapp/sessions`. Keyed on the `[e2e-stub]` banner the lightweight
    # PTY child prints (#534), which only this harness can produce — so a
    # real session written by the live tray during the gate can never
    # trip it, and the check needs no exclusive access to the directory.
    leaked = leaked_stub_sessions(SESSIONS_DIR, iso.run_started_at)
    if leaked:
        raise RuntimeError(
            f"e2e autoboot isolation breach: at least {len(leaked)} harness "
            f"session file(s) landed in {SESSIONS_DIR} during the run "
            f"instead of the temp audit dir ({iso.audit_dir}) — e.g. "
            f"{leaked[0].name}. Every process this fixture spawns must get "
            f"{AUDIT_DIR_ENV} (issue #913)."
        )


def _assert_no_leaked_uploads(iso: Isolation) -> None:
    # Upload isolation regression check (issue #922): no file this harness
    # uploaded may have landed in the checkout's real `.launcher-tmp`.
    # Keyed on the `UPLOAD_MARKER` filename prefix every harness upload
    # carries, so a photo the user attaches on the live tray during the
    # gate can never trip it.
    leaked_uploads = leaked_stub_uploads(UPLOADS_DIR, iso.run_started_at)
    if leaked_uploads:
        raise RuntimeError(
            f"e2e autoboot isolation breach: at least {len(leaked_uploads)} "
            f"harness upload(s) landed in {UPLOADS_DIR} during the run "
            f"instead of the temp upload root ({iso.uploads_root}) — e.g. "
            f"{leaked_uploads[0].name}. The disposable session-host must "
            f"get {UPLOAD_ROOT_ENV} (issue #922)."
        )
