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


def _wrapper_bat_content(tray_bat: Path) -> str:
    """A one-line launcher: cd into the repo, then call tray.bat.

    ``tray.bat`` is idempotent (no-op if a tray is already running), so a
    Startup-folder run racing an already-running tray (e.g. a prior manual
    launch) is safe.
    """
    return (
        "@echo off\r\n"
        f'cd /d "{tray_bat.parent}"\r\n'
        f'call "{tray_bat}"\r\n'
    )


def is_enabled(startup_dir: Optional[Path] = None) -> bool:
    """Whether the boot-autostart wrapper bat currently exists."""
    return wrapper_bat_path(startup_dir).is_file()


def enable(*, tray_bat: Path = TRAY_BAT_PATH, startup_dir: Optional[Path] = None) -> Path:
    """Write the wrapper bat into the Startup folder. Returns its path."""
    target_dir = startup_dir if startup_dir is not None else _startup_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / STARTUP_BAT_NAME
    path.write_text(_wrapper_bat_content(tray_bat), encoding="utf-8")
    return path


def disable(startup_dir: Optional[Path] = None) -> bool:
    """Remove the wrapper bat. Returns ``True`` if it existed and was removed."""
    path = wrapper_bat_path(startup_dir)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
