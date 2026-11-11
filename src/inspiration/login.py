"""Settings › Inspiration: the X login window, started on this PC.

``scan.py login-x`` (fleet-config's inspiration-scan skill) opens the dedicated
X Chrome profile headed and returns when the owner closes the window. This
module starts it as a detached child under fleet-config's own Python and
reports whether the window is open, closed or failed. No credential passes
through here: the owner logs in inside that window.

:class:`LoginState` is the closed set ``state`` takes, declared once; the
browser's copy is ``app/webapp/static/settings/login-state.js``.
:func:`exit_state` is the one rule that maps the child's exit code to a state.

One login at a time: a second start while one is open reports the open one.
The state lives in this process only; after a webapp restart the child keeps
running and the status reads ``idle``, which is the honest answer (not known).
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import threading
import time
from enum import StrEnum
from pathlib import Path
from typing import IO, Any, Dict, Optional

from src.inspiration.sources import SKILL_SUBPATH
from src.subprocess_flags import NO_WINDOW

logger = logging.getLogger(__name__)


class LoginState(StrEnum):
    """Every value the X login window's ``state`` may hold."""

    #: No window started since this process began (or the start is not known after a restart).
    IDLE = "idle"
    #: The window is open; a second start reports this one.
    RUNNING = "running"
    #: The owner closed the window and the helper exited cleanly.
    CLOSED = "closed"
    #: The helper exited non-zero; ``detail`` carries its last stderr line.
    FAILED = "failed"


def exit_state(code: int) -> LoginState:
    """The state a finished login helper leaves: only a clean exit is a closed window."""
    return LoginState.CLOSED if code == 0 else LoginState.FAILED


_MAX_DETAIL = 300

_lock = threading.Lock()
_proc: Optional["subprocess.Popen[bytes]"] = None
_stderr: Optional[IO[bytes]] = None
_started_at: float = 0.0
_ended_at: float = 0.0
_result: Dict[str, Any] = {"state": LoginState.IDLE, "detail": ""}


class LoginStartError(Exception):
    """The login window could not be started; the message says why."""


def _python(fleet_config_dir: Path) -> Path:
    return fleet_config_dir / ".venv" / "Scripts" / "python.exe"


def _last_line(handle: Optional[IO[bytes]]) -> str:
    if handle is None:
        return ""
    try:
        handle.seek(0)
        lines = [ln.strip() for ln in handle.read().decode("utf-8", errors="replace").splitlines() if ln.strip()]
    except OSError:
        return ""
    return lines[-1][:_MAX_DETAIL] if lines else ""


def _settle() -> None:
    """Fold a finished child into ``_result``. Caller holds the lock."""
    global _proc, _stderr, _ended_at, _result
    if _proc is None:
        return
    code = _proc.poll()
    if code is None:
        return
    detail = "" if code == 0 else (_last_line(_stderr) or f"The login helper stopped with exit code {code}.")
    state = exit_state(code)
    _result = {"state": state, "detail": detail}
    _ended_at = time.time()
    if _stderr is not None:
        _stderr.close()
    _proc, _stderr = None, None
    logger.info("ℹ️ X login window %s (exit %s)", state.value, code)


def status() -> Dict[str, Any]:
    """``{state, detail, started_at, ended_at}``; ``state`` is a :class:`LoginState`."""
    with _lock:
        _settle()
        running = _proc is not None
        return {
            "state": LoginState.RUNNING if running else _result["state"],
            "detail": "" if running else _result["detail"],
            "started_at": _started_at or None,
            "ended_at": None if running else (_ended_at or None),
        }


def start(fleet_config_dir: str) -> Dict[str, Any]:
    """Open the login window; returns :func:`status`. Raises :class:`LoginStartError`."""
    global _proc, _stderr, _started_at, _result
    root = Path(fleet_config_dir) if str(fleet_config_dir or "").strip() else None
    if root is None:
        raise LoginStartError("The fleet-config folder is not set in Settings.")
    script = root / SKILL_SUBPATH / "scan.py"
    python = _python(root)
    if not script.is_file():
        raise LoginStartError("This fleet-config checkout has no inspiration-scan skill yet.")
    if not python.is_file():
        raise LoginStartError("fleet-config has no Python environment to run the login helper.")
    with _lock:
        _settle()
        if _proc is not None:
            return _locked_status()
        stderr = tempfile.TemporaryFile()
        env = {**os.environ, "PYTHONUTF8": "1"}
        try:
            _proc = subprocess.Popen(
                [str(python), str(script), "login-x"], cwd=str(root), env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr,
                creationflags=NO_WINDOW,
            )
        except OSError as exc:
            stderr.close()
            logger.warning("⚠️ X login window did not start: %s", exc.strerror or exc)
            raise LoginStartError(f"The login helper did not start: {exc.strerror or exc}") from exc
        _stderr = stderr
        _started_at = time.time()
        _result = {"state": LoginState.RUNNING, "detail": ""}
        logger.info("ℹ️ X login window started")
        return _locked_status()


def _locked_status() -> Dict[str, Any]:
    return {"state": LoginState.RUNNING, "detail": "", "started_at": _started_at or None, "ended_at": None}
