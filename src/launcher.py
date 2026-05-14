"""Process-launch helpers — spawn ``*.bat`` files or raw ``claude`` invocations.

Used by the FastAPI ``/api/apps/{id}/launch`` route. Two flavours:

- ``spawn_bat`` opens a new visible CMD window that runs the bat and
  stays open afterwards (so the user can interact when they're back at
  the PC).
- ``spawn_claude`` opens a new visible CMD window, cwd's into the
  project folder, and runs ``claude <flags>`` directly — no bat file
  required. The window closes when ``claude`` exits.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def spawn_bat(bat_path: Path) -> None:
    """Open a new visible CMD window that runs the bat and stays open."""
    if not bat_path.is_file():
        raise OSError(f"BAT file not found: {bat_path}")
    cmd = ["cmd", "/c", "start", "", "cmd", "/k", str(bat_path)]
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_CONSOLE  # type: ignore[attr-defined]
    subprocess.Popen(
        cmd,
        cwd=str(bat_path.parent),
        shell=False,
        creationflags=creationflags,
        close_fds=True,
    )
    logger.info(f"🚀 spawned bat: {bat_path}")


def spawn_claude(project_dir: Path, flags: str) -> None:
    """Open a new visible CMD window running ``claude <flags>`` in ``project_dir``.

    Uses ``cmd /c`` (not ``/k``) so the window closes when claude exits —
    no double-exit. The outer Popen's ``cwd`` is inherited by ``start``.
    """
    if not project_dir.is_dir():
        raise OSError(f"Project directory not found: {project_dir}")
    cmd = ["cmd", "/c", "start", "", "cmd", "/c", f"claude {flags}"]
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_CONSOLE  # type: ignore[attr-defined]
    subprocess.Popen(
        cmd,
        cwd=str(project_dir),
        shell=False,
        creationflags=creationflags,
        close_fds=True,
    )
    logger.info(f"🚀 spawned claude in {project_dir} with: {flags}")
