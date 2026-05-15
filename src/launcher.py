"""Process-launch helpers — spawn ``*.bat`` files or ``claude`` sessions.

Used by the FastAPI ``/api/apps/{id}/launch`` route:

- ``spawn_bat`` opens a new visible CMD window that runs the bat and
  stays open afterwards (so the user can interact when they're back at
  the PC).
- ``spawn_claude_session`` covers both Claude Code launch modes via its
  ``kind`` argument. ``kind="pty"`` (full control) asks the loopback
  session-host to run ``claude`` inside a launcher-owned ConPTY, streamed
  to and driven from the phone. ``kind="remote"`` (detached) asks the
  session-host to spawn ``claude`` in its own console window the launcher
  only tracks — visible on the PC, killable from the phone, but not
  streamed; the Claude cloud app drives it.
- ``open_local_terminal_window`` opens the PC-side mirror window for a
  full-control session.

The Claude Code tab chooses the mode via the ``/api/apps/{id}/launch``
``mode`` parameter (``pty`` | ``remote``).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Any, Dict

from src import session_client

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


def spawn_claude_session(
    project_dir: Path,
    name: str,
    flags: str,
    session_host_port: int,
    kind: str = "pty",
) -> Dict[str, Any]:
    """Ask the session-host to run ``claude <flags>`` for ``project_dir``.

    ``kind="pty"`` (default) spawns a launcher-owned ConPTY streamed to the
    phone; ``kind="remote"`` spawns a detached console window the host only
    tracks. Returns the new session's API dict (``session_id``, ``kind``,
    ``name``, …). Raises :class:`session_client.SessionHostError` when the
    session-host is down or rejects the request — the caller surfaces that
    to the UI.
    """
    if not project_dir.is_dir():
        raise OSError(f"Project directory not found: {project_dir}")
    session = session_client.create_session(
        session_host_port, str(project_dir), name, flags, kind=kind
    )
    logger.info(
        f"🚀 spawned claude {kind} session "
        f"{str(session.get('session_id'))[:8]} in {project_dir}"
    )
    return session


# Edge / Chrome installs to probe for `--app` (chromeless) window mode.
_BROWSER_APP_CANDIDATES = (
    (r"Microsoft\Edge\Application\msedge.exe", "ProgramFiles(x86)"),
    (r"Microsoft\Edge\Application\msedge.exe", "ProgramFiles"),
    (r"Google\Chrome\Application\chrome.exe", "ProgramFiles"),
    (r"Google\Chrome\Application\chrome.exe", "ProgramFiles(x86)"),
)


def open_local_terminal_window(url: str) -> None:
    """Open ``url`` in a window on the PC — the launcher-owned terminal mirror.

    Prefers Edge/Chrome ``--app`` mode for a clean, dedicated window that
    feels like a terminal rather than a browser tab; falls back to the
    default browser. ``--ignore-certificate-errors`` is passed because the
    window only ever points at this launcher's own loopback origin, whose
    self-signed cert is intentionally not trusted on the PC — that risk
    doesn't apply to ``127.0.0.1``. ``--test-type`` suppresses the flag
    warning bar. Best-effort — a failure here never breaks the launch.
    """
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for rel, env_key in _BROWSER_APP_CANDIDATES:
        base = os.environ.get(env_key)
        if not base:
            continue
        exe = Path(base) / rel
        if not exe.is_file():
            continue
        try:
            subprocess.Popen(
                [
                    str(exe),
                    f"--app={url}",
                    "--window-size=1024,720",
                    "--ignore-certificate-errors",
                    "--test-type",
                ],
                creationflags=creationflags,
                close_fds=True,
            )
            logger.info(f"🖥️  opened local terminal window: {url}")
            return
        except OSError as exc:
            logger.debug(f"--app launch via {exe} failed: {exc}")
    try:
        webbrowser.open(url)
        logger.info(f"🖥️  opened local terminal window (default browser): {url}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"⚠️  could not open local terminal window: {exc}")
