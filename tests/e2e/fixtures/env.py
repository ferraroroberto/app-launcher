"""Paths, env-var names and run state the e2e fixtures share."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
WEBAPP_CONFIG = REPO_ROOT / "config" / "webapp_config.json"
SESSIONS_DIR = REPO_ROOT / "webapp" / "sessions"
BASE_URL = "https://127.0.0.1:8445"
TOKEN_KEY = "launcher.token"  # must match TOKEN_KEY in app/webapp/static/state.js

# The live tray's loopback PTY session-host port. Autoboot must NEVER adopt a
# host listening here: on a dev box it holds the user's real PTY/Claude
# sessions — including the one running the gate — and the destructive e2e
# tests would kill them (issue #260). Instead autoboot always spawns its own
# disposable session-host on a free port and points the disposable webapp at
# it via LAUNCHER_SESSION_HOST_PORT. This constant is kept only so the
# isolation guarantee can be asserted (never bound/adopted under autoboot).
LIVE_SESSION_HOST_PORT = 8446
# Env var the webapp honours to override its session-host port (see
# src/webapp_config.py:SESSION_HOST_PORT_ENV) — the injection that isolates
# the gate from the live :8446.
SESSION_HOST_PORT_ENV = "LAUNCHER_SESSION_HOST_PORT"
# Env var the webapp honours to override its config file *path* (see
# src/webapp_config.py:WEBAPP_CONFIG_PATH_ENV) — the injection that stops a
# Settings-tab e2e Save from ever mutating the user's real
# config/webapp_config.json (issue #441; the #438 port corruption was this
# exact shared-file design biting). Autoboot points the disposable webapp at
# its own temp config, derived from the real one so it still boots with
# realistic values but carrying no credential (issue #907).
WEBAPP_CONFIG_PATH_ENV = "LAUNCHER_WEBAPP_CONFIG"
# Env var the webapp honours to override the boot-autostart Startup directory
# (see src/boot_autostart.py:STARTUP_DIR_ENV) — the injection that stops the
# boot-autostart e2e test from ever reading/writing the real per-user Startup
# folder (issue #698). Without it, `/api/settings/boot-autostart` resolves
# the real folder, so the test could only pass on a host with no
# AppLauncher.bat installed there for real login-time autostart.
STARTUP_DIR_ENV = "LAUNCHER_STARTUP_DIR"
# Env var both the webapp and the session-host honour to relocate the audit
# runtime dir (see src/audit.py:AUDIT_DIR_ENV) — the injection that stops a
# gate run from writing its throwaway `<sid>.log` / `<sid>.transcript` pairs
# into the checkout's live `webapp/sessions` (issue #913). Before it, a gate
# in the primary checkout wrote its whole run straight into the directory the
# user's phone reads — measured at 228 files (114 sessions) for one full
# dual-projection run — because src/audit.py resolves that path from
# `__file__`, so pointing the disposable processes at a temp *config* (#911)
# never moved their session files.
AUDIT_DIR_ENV = "LAUNCHER_AUDIT_DIR"
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
UPLOAD_ROOT_ENV = "LAUNCHER_UPLOAD_ROOT"
UPLOADS_DIR = REPO_ROOT / ".launcher-tmp"
# Filename marker every harness upload carries, so the teardown breach check
# below accuses only on files this suite wrote. Same reasoning as the
# `[e2e-stub]` transcript banner: a real photo the user sends from the phone
# to the live tray mid-run must never trip the check.
UPLOAD_MARKER = "e2e-stub-"
# The `gh` the disposable webapp runs (#1516). Any test that opens the Board
# without mocking it sets off the GitHub refresh (a never-filled cache heals
# on the first poll, #910), so one profiled gate run went out to real GitHub
# 15 times: 45 `gh.exe` spawns. `src.clients.github` honours
# `LAUNCHER_GH_CMD` for exactly this; the synthetic `gh` the design-review
# instance already uses answers the three searches with a few public-safe
# rows, so the Backlog still has cards to lay out and nothing reaches GitHub.
SYNTHETIC_GH_CMD = json.dumps([sys.executable, str(REPO_ROOT / "scripts" / "synthetic_gh.py")])
AUTOBOOT_ENV = "LAUNCHER_E2E_AUTOBOOT"
# Filled by _autoboot_server so the lightweight fixture can create sessions
# directly on the disposable session-host (the sentinel flag can't travel
# through the webapp's launch endpoint, which builds flags from config).
AUTOBOOT_STATE: dict = {}
# Explicit opt-in for targeting the LIVE tray on :8445 (issue #386). The
# live mode is deliberate (run-e2e.ps1 dev loop), but it drives real login
# flows and PTY sessions against the instance the user's phone is using —
# an *accidental* bare `pytest tests/e2e` must not do that.
LIVE_ENV = "LAUNCHER_E2E_LIVE"

# Parallel workers (#1231): under pytest-xdist every worker is its own pytest
# process booting its own disposable webapp + session-host, so what was one
# run's private state — a fixed log name, a port picked and then released —
# is now shared between workers. "" when the run is not distributed.
XDIST_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "")


def autoboot_enabled(config: pytest.Config) -> bool:
    return bool(config.getoption("--e2e-autoboot")) or (
        os.environ.get(AUTOBOOT_ENV, "") == "1"
    )
