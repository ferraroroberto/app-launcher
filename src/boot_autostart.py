"""App-launcher's own boot-at-log-on toggle (issue #456, part 1/2).

The README's manual recipe (`Auto-start at log on with Task Scheduler`)
creates an "At log on" scheduled task pointing at ``tray.bat``. Reproducing
that programmatically from the webapp process was tried and reverted:
``schtasks /Create /SC ONLOGON`` returns "Access is denied" from an
unelevated process (empirically verified) — Windows gates the ONLOGON/
ONSTART trigger types behind elevation, unlike the Jobs tab's time-based
schedules (``DAILY``/``HOURLY``/…) which `src.jobs_schtasks` creates fine
from this same unprivileged process.

Instead this drops a tiny wrapper ``.bat`` into the current user's own
Startup folder (``%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\
Startup``) — a plain file write under the user's own profile, no
elevation needed, and the standard no-admin mechanism Windows itself
offers for per-user login autostart (other installed apps, e.g. Telegram,
already use it on this machine).

Diagnosability (issue #582): a login-time launch that dies silently is
un-debuggable, so the wrapper is not a bare ``call tray.bat`` — it writes
a timestamped breadcrumb, redirects ``tray.bat``'s own output, and logs
its exit code to ``webapp\\startup.log`` (gitignored via ``*.log``) on
every login attempt, so a future failure is diagnosable from that log
alone. It also checks the one hard precondition that makes ``tray.bat``
``exit /b 1`` — the shared ``tray_lifecycle.ps1`` helper being absent
(a machine-config gap, not something a retry could fix) — and records it.
No network/Tailscale dependency exists on this path: the Startup wrapper
calls ``tray.bat`` *without* ``--restart``, and the lifecycle helper's
plain-``launch`` action only detects local processes and starts a local
pythonw — it performs git/HTTP verification solely under ``-Restart`` —
so late Tailscale cannot make the login launch fail, and no retry loop is
warranted (the breadcrumb is what would justify one later, with evidence).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TRAY_BAT_PATH = PROJECT_ROOT / "tray.bat"

STARTUP_BAT_NAME = "AppLauncher.bat"


def _startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("APPDATA environment variable is not set")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def wrapper_bat_path(startup_dir: Optional[Path] = None) -> Path:
    """The wrapper bat's path — under ``startup_dir`` when given (tests), else
    the real per-user Startup folder."""
    return (startup_dir if startup_dir is not None else _startup_dir()) / STARTUP_BAT_NAME


def _startup_log_path(repo_root: Path) -> Path:
    """The gitignored login breadcrumb log this repo writes at each autostart."""
    return repo_root / "webapp" / "startup.log"


def _wrapper_bat_content(tray_bat: Path) -> str:
    """A self-logging launcher: cd into the repo, then call tray.bat, leaving a
    timestamped trail in ``webapp\\startup.log`` for every login attempt.

    ``tray.bat`` is idempotent (no-op if a tray is already running), so a
    Startup-folder run racing an already-running tray (e.g. a prior manual
    launch) is safe. The breadcrumb + redirected ``tray.bat`` output + logged
    exit code exist because a login-time failure was otherwise silent (issue
    #582). The precondition check mirrors the one hard-fail in ``tray.bat``
    (missing shared ``tray_lifecycle.ps1`` → ``exit /b 1``) so that
    machine-config gap is named in the log rather than showing up as a bare
    non-zero exit code.
    """
    repo_root = tray_bat.parent
    log = _startup_log_path(repo_root)
    helper = r"%USERPROFILE%\.claude\tray\tray_lifecycle.ps1"
    return (
        "@echo off\r\n"
        f'cd /d "{repo_root}"\r\n'
        f'>>"{log}" echo [%date% %time%] autostart wrapper fired (cwd "%CD%")\r\n'
        f'if not exist "{helper}" '
        f'>>"{log}" echo [%date% %time%] WARNING precondition missing: '
        f'shared tray helper "{helper}" not found - tray.bat will exit 1 '
        f"(install fleet-config and run its install.ps1)\r\n"
        f'call "{tray_bat}" >>"{log}" 2>&1\r\n'
        f'>>"{log}" echo [%date% %time%] tray.bat returned errorlevel %ERRORLEVEL%\r\n'
    )


def is_enabled(startup_dir: Optional[Path] = None) -> bool:
    """Whether the boot-autostart wrapper bat currently exists."""
    return wrapper_bat_path(startup_dir).is_file()


def enable(*, tray_bat: Path = TRAY_BAT_PATH, startup_dir: Optional[Path] = None) -> Path:
    """Write the wrapper bat into the Startup folder. Returns its path.

    Verifies the write actually landed (read-back must match the intended
    content) and raises ``OSError`` otherwise, so a partial/failed write
    surfaces to the caller (the ``/api/settings/boot-autostart`` route turns
    it into a 400) instead of silently reporting success — the failure mode
    behind issue #582 was exactly a silent no-op.
    """
    target_dir = startup_dir if startup_dir is not None else _startup_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / STARTUP_BAT_NAME
    # Binary I/O so the CRLF line endings land verbatim: text-mode write would
    # translate each "\n" to os.linesep, turning our "\r\n" into "\r\r\n" on
    # disk, and text-mode read-back would then differ from what we wrote,
    # making the verification below spuriously fail.
    data = _wrapper_bat_content(tray_bat).encode("utf-8")
    path.write_bytes(data)
    try:
        written = path.read_bytes()
    except OSError as exc:
        raise OSError(f"autostart wrapper written but unreadable at {path}: {exc}") from exc
    if written != data:
        raise OSError(
            f"autostart wrapper write did not land intact at {path} "
            f"(read back {len(written)} bytes, expected {len(data)})"
        )
    return path


def disable(startup_dir: Optional[Path] = None) -> bool:
    """Remove the wrapper bat. Returns ``True`` if it existed and was removed."""
    path = wrapper_bat_path(startup_dir)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
