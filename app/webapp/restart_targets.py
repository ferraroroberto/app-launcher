"""What the page-foot restart sheet may restart here, and who may ask.

The vendored ``src/self_restart.py`` owns the routes, the detached runner and
the run record. This module declares this app's two targets and the guard:

* **Restart app** runs the repo's own ``tray.bat --restart``.
* **Restart session-host** runs ``scripts/restart-session-host.ps1 -Confirm``,
  which ends every live terminal session. The sheet asks twice for a target
  with a warning, and this warning names how many live sessions it will end.
  It is a callable the vendored router runs on every ``GET /api/restart`` and
  the sheet re-reads each time it opens, so the count is current at open time.

Both targets sit behind one guard: a restart is a terminal-grade action (the
session-host one ends every PTY), so the ``POST`` takes the same Tailscale +
passkey gate as the routes that write into a live PTY, and the PC itself
(loopback) passes as it does there. The ``GET`` stays on the bearer token the
middleware already checked, so the page can draw the button without a passkey
ceremony at load.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, FastAPI, HTTPException, Request

from src import self_restart
from src.clients import session as session_client
from src.middleware import trust
from src.middleware.terminal_gate import terminal_http_gate
from src.runtime_data import runtime_data_dir
from src.self_restart import TRAY_RESTART, RestartTarget, restart_router

logger = logging.getLogger(__name__)

APP_TARGET = RestartTarget("app", "Restart app", TRAY_RESTART)
SESSION_HOST_TARGET_ID = "session-host"
SESSION_HOST_LABEL = "Restart session-host"


def session_host_command() -> tuple[str, ...]:
    """The repo's one supported session-host restart, run from the checkout root.

    The ``.ps1`` goes through ``src.launching.ps_script`` so it starts under
    any execution policy, as the script's own usage line spells it.
    """
    return (
        sys.executable, "-m", "src.launching.ps_script",
        "scripts\\restart-session-host.ps1", "-Confirm",
    )


def count_live_sessions(port: int) -> Optional[int]:
    """Live sessions on the session-host, or ``None`` when it could not be read."""
    try:
        sessions = session_client.list_sessions(port)
    except session_client.SessionHostError as exc:
        logger.warning("⚠️ restart sheet: live session count unknown: %s", exc)
        return None
    return sum(1 for s in sessions if s.get("alive", True))


def session_host_warning(count: Optional[int]) -> str:
    """The second-ask text: how many live sessions the restart ends."""
    if count is None:
        return ("This ends every live terminal session on this PC, the chief included. "
                "The number of live sessions could not be read.")
    if count == 0:
        return "No terminal session is live. The session-host and the app restart."
    noun = "session" if count == 1 else "sessions"
    return (f"This ends {count} live terminal {noun} on this PC, the chief included. "
            "There is no drain: each one stops at once.")


def restart_guard(request: Request) -> None:
    """Refuse a restart ``POST`` without loopback, or the tailnet and a passkey."""
    if request.method != "POST":
        return
    client_host = request.client.host if request.client else ""
    if trust.is_pc_itself(client_host, request.headers):
        return
    refused = terminal_http_gate(request, level="passkey")
    if refused is not None:
        detail = json.loads(bytes(refused.body)).get("detail", "restart refused")
        logger.warning("⚠️ restart refused for %s: %s", client_host, detail)
        raise HTTPException(status_code=refused.status_code, detail=detail)


def _spawn_runner(spec_path: Path, root: Path) -> None:
    """The vendored detached spawn, looked up per call so a test can stub it."""
    self_restart.spawn_detached(spec_path, root)


def build_restart_router(app: FastAPI) -> APIRouter:
    """``GET``/``POST /api/restart`` for this app's two targets, behind :func:`restart_guard`.

    The session-host port is read from ``app.state.webapp_config`` per count,
    so a config reload is followed.
    """
    targets = [
        APP_TARGET,
        RestartTarget(
            SESSION_HOST_TARGET_ID, SESSION_HOST_LABEL, session_host_command(),
            warning=lambda: session_host_warning(
                count_live_sessions(app.state.webapp_config.session_host_port)),
        ),
    ]
    router = restart_router(
        targets=targets,
        status_dir=runtime_data_dir("app-launcher", create=True) / "restart",
        guard=restart_guard,
        spawn=_spawn_runner,
    )
    return router
