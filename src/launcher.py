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
  full-control session and (optionally) tracks its HWND so Stop & Close
  can dismiss it via WM_CLOSE — issue #20.

The Claude Code tab chooses the mode via the ``/api/apps/{id}/launch``
``mode`` parameter (``pty`` | ``remote``).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from src import session_client

logger = logging.getLogger(__name__)

# WM_CLOSE from winuser.h. Inlined so callers don't need to import
# win32con just to dismiss a window.
_WM_CLOSE = 0x0010

# Title the PC mirror page sets on load (see terminal.js). EnumWindows
# matches on this to find the Edge --app window that hosts the mirror —
# the spawned msedge.exe PID hands the request off to an existing Edge
# instance, so the PID alone is useless for closing the window.
_MIRROR_TITLE_PREFIX = "app-launcher-mirror-"

# How long to poll for the mirror window's HWND after spawn (seconds).
# Edge can take ~1 s to show the window + run JS that sets the title;
# 3 s is comfortable headroom on a cold launch.
_HWND_POLL_BUDGET_SECONDS = 3.0
_HWND_POLL_INTERVAL_SECONDS = 0.1

# sid → HWND of the Edge --app mirror window. Populated by the post-spawn
# polling thread; consumed by ``close_mirror_window`` on Stop & Close.
# Module-level (single launcher process) — no lock needed: writes happen
# from one polling thread per sid, reads happen from the FastAPI stop
# route, and dict ops are atomic under the GIL for single-key updates.
_mirror_hwnds: Dict[str, int] = {}


def spawn_bat(bat_path: Path) -> int:
    """Open a new visible CMD window that runs the bat and stays open.

    Returns the PID of the spawned ``cmd /c start`` launcher process. The
    actual app server runs as a descendant of it — the caller pairs this
    PID with :func:`src.diagnostics.listening_port_for_pid_tree` to learn
    which port the app eventually bound.
    """
    if not bat_path.is_file():
        raise OSError(f"BAT file not found: {bat_path}")
    # `cmd /k` runs the bat and keeps the window open. Spawned directly
    # (not via `start`) so the new console's cmd.exe stays a *child* of
    # this process — `start` would detach it and orphan the descendant
    # tree, breaking the port-discovery walk in app_runtime.
    cmd = ["cmd", "/k", str(bat_path)]
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_CONSOLE  # type: ignore[attr-defined]
    proc = subprocess.Popen(
        cmd,
        cwd=str(bat_path.parent),
        shell=False,
        creationflags=creationflags,
        close_fds=True,
    )
    logger.info(f"🚀 spawned bat: {bat_path} (pid {proc.pid})")
    return proc.pid


def spawn_claude_session(
    project_dir: Path,
    name: str,
    flags: str,
    session_host_port: int,
    kind: str = "pty",
    agent: str = "claude",
    rows: int = 40,
    cols: int = 120,
) -> Dict[str, Any]:
    """Ask the session-host to run ``<agent> <flags>`` for ``project_dir``.

    ``agent`` selects the coding CLI (``claude`` | ``antigravity``).
    ``kind="pty"`` (default) spawns a launcher-owned ConPTY streamed to the
    phone; ``kind="remote"`` spawns a detached console window the host only
    tracks. ``rows``/``cols`` are the phone's real terminal dimensions, so a
    ``pty`` session paints its first frame at the right width (issue #126);
    they are ignored for ``remote`` (no PTY). Returns the new session's API
    dict (``session_id``, ``kind``, ``agent``, ``name``, …). Raises
    :class:`session_client.SessionHostError` when the session-host is down or
    rejects the request — the caller surfaces that to the UI.
    """
    if not project_dir.is_dir():
        raise OSError(f"Project directory not found: {project_dir}")
    session = session_client.create_session(
        session_host_port, str(project_dir), name, flags,
        kind=kind, agent=agent, rows=rows, cols=cols,
    )
    logger.info(
        f"🚀 spawned {agent} {kind} session "
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


def open_local_terminal_window(url: str, sid: Optional[str] = None) -> None:
    """Open ``url`` in a window on the PC — the launcher-owned terminal mirror.

    Prefers Edge/Chrome ``--app`` mode for a clean, dedicated window that
    feels like a terminal rather than a browser tab; falls back to the
    default browser. ``--ignore-certificate-errors`` is passed because the
    window only ever points at this launcher's own loopback origin, whose
    self-signed cert is intentionally not trusted on the PC — that risk
    doesn't apply to ``127.0.0.1``. ``--test-type`` suppresses the flag
    warning bar. Best-effort — a failure here never breaks the launch.

    When ``sid`` is provided, spawn a background thread that polls
    ``EnumWindows`` for a top-level window whose title contains
    ``app-launcher-mirror-<sid>`` (set by the mirror page on load) and
    stashes its HWND so :func:`close_mirror_window` can post
    ``WM_CLOSE`` to it on Stop & Close — issue #20.
    """
    _spawn_terminal_window(url)
    if sid:
        logger.debug(
            f"starting mirror HWND lookup for sid {sid[:8]} "
            f"(title prefix '{_MIRROR_TITLE_PREFIX}')"
        )
        _run_in_thread(lambda: _safe_poll(sid))


def _spawn_terminal_window(url: str) -> None:
    """Spawn the Edge/Chrome --app window for ``url``, with browser fallback."""
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


def _run_in_thread(fn: Callable[[], None]) -> None:
    """Spawn a daemon thread that runs ``fn``. Seam for tests to run inline."""
    threading.Thread(target=fn, name="mirror-hwnd-poll", daemon=True).start()


# ----------------------------------------------------------- HWND tracking


def register_mirror_hwnd(sid: str, hwnd: int) -> None:
    """Stash the mirror window's HWND for later WM_CLOSE."""
    _mirror_hwnds[sid] = hwnd
    logger.debug(
        f"registered mirror HWND {hwnd} for sid {sid[:8]} "
        f"(registry now: {len(_mirror_hwnds)} entries)"
    )


def forget_mirror_hwnd(sid: str) -> None:
    """Drop any HWND stashed for ``sid`` — safe if none was ever stored."""
    _mirror_hwnds.pop(sid, None)


def close_mirror_window(sid: str) -> bool:
    """Post ``WM_CLOSE`` to the mirror window for ``sid``.

    Returns ``True`` when a message was successfully posted (the window
    will then close itself). Returns ``False`` when no HWND was stashed
    (window never opened, launch came from the PC itself, or the
    title-set race lost) or when ``PostMessage`` failed (the window was
    already gone — manually closed, crashed, …). Either way the entry
    is dropped from the registry so a stale HWND can't be retried.
    """
    hwnd = _mirror_hwnds.pop(sid, None)
    if hwnd is None:
        logger.debug(
            f"close_mirror_window({sid[:8]}): no HWND stashed — "
            f"registry has {len(_mirror_hwnds)} other entries"
        )
        return False
    try:
        import win32gui  # type: ignore
    except ImportError:
        logger.warning(
            "pywin32 not available — mirror window close skipped for "
            f"sid {sid[:8]}"
        )
        return False
    try:
        win32gui.PostMessage(hwnd, _WM_CLOSE, 0, 0)
        logger.debug(
            f"PostMessage(WM_CLOSE) → hwnd {hwnd} for sid {sid[:8]}"
        )
        return True
    except Exception as exc:  # noqa: BLE001 — pywintypes.error et al.
        logger.warning(
            f"🪟 PostMessage(WM_CLOSE) → hwnd {hwnd} for sid {sid[:8]} "
            f"failed: {exc}"
        )
        return False


def _safe_poll(sid: str) -> None:
    """Wrap ``_poll_for_mirror_hwnd`` so a thread-killing exception is logged.

    A bare ``Thread(target=fn)`` swallows any exception that escapes ``fn``
    silently, which would leave us debugging a registry that's mysteriously
    empty. Catch everything here and log it instead.
    """
    try:
        _poll_for_mirror_hwnd(sid)
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"⚠️  mirror HWND poll thread crashed for sid {sid[:8]}: {exc}")


def _poll_for_mirror_hwnd(sid: str) -> None:
    """Find the top-level window whose title contains ``app-launcher-mirror-<sid>``.

    Polls ``EnumWindows`` every ~100 ms for up to ~3 s; first match wins.
    The mirror page sets its ``document.title`` on load, so we can't
    expect the window to exist immediately after spawning Edge. Uses a
    substring match rather than exact equality because some browser
    modes (PWA, certain Edge channels) prepend the app name to the
    page title.
    """
    try:
        import win32gui  # type: ignore
    except ImportError:
        logger.warning(
            "pywin32 not available — mirror window auto-close disabled"
        )
        return

    target = _MIRROR_TITLE_PREFIX + sid
    deadline = time.monotonic() + _HWND_POLL_BUDGET_SECONDS
    attempts = 0
    last_titles: list[str] = []

    while time.monotonic() < deadline:
        attempts += 1
        found: list[int] = []
        # Snapshot enumerated titles for the diagnostic dump on timeout.
        # Only kept for the latest sweep so the log line stays bounded.
        last_titles = []

        def _cb(hwnd: int, _extra: Any) -> bool:
            if found:
                return False
            try:
                title = win32gui.GetWindowText(hwnd) or ""
            except Exception:  # noqa: BLE001
                return True
            if title:
                last_titles.append(title)
            if target in title:
                found.append(hwnd)
                return False
            return True

        try:
            win32gui.EnumWindows(_cb, None)
        except Exception as exc:  # noqa: BLE001 — pywintypes.error
            logger.debug(f"EnumWindows failed (attempt {attempts}): {exc}")

        if found:
            logger.debug(
                f"found mirror HWND {found[0]} for sid {sid[:8]} "
                f"after {attempts} sweep(s)"
            )
            register_mirror_hwnd(sid, found[0])
            return

        time.sleep(_HWND_POLL_INTERVAL_SECONDS)

    # Timeout — show a sample of what WAS enumerated so we can see if the
    # title is being set differently than expected (Edge wrapping it,
    # mirror page bypassing the isMirror branch, …).
    sample = [t for t in last_titles if t][:8]
    logger.warning(
        f"⚠️  mirror HWND lookup timed out for sid {sid[:8]} after "
        f"{attempts} sweep(s) — no top-level window title contained "
        f"'{target}'. Sample of titles seen in the last sweep: {sample}"
    )
