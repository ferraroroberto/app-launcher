"""Process-launch helpers — spawn ``*.bat`` files or coding-agent sessions.

Used by the FastAPI ``/api/apps/{id}/launch`` route:

- ``spawn_bat`` opens a new visible CMD window that runs the bat and
  stays open afterwards (so the user can interact when they're back at
  the PC).
- ``spawn_claude_session`` is the multi-agent session-launch helper for
  the Coding tab. It asks the loopback session-host to run whichever
  coding agent is requested (see :mod:`src.agents` for the full set).
  ``kind="pty"`` (full control) streams the session to and drives it
  from the phone inside a launcher-owned ConPTY. ``kind="remote"``
  (detached) opens a console window on the PC that the session-host only
  tracks — visible on the PC, killable from the phone, but not streamed.
- ``open_local_terminal_window`` opens the PC-side mirror window for a
  full-control session and (optionally) tracks its HWND so Stop & Close
  can dismiss it via WM_CLOSE — issue #20.

The Coding tab chooses the mode via the ``/api/apps/{id}/launch``
``mode`` parameter (``pty`` | ``remote``) and the agent via ``agent``.
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
from typing import Any, Callable, Dict, Iterable, List, Optional

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

    ``agent`` selects the coding CLI; see :data:`src.agents.AGENTS`.
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


def _post_wm_close(win32gui: Any, hwnd: int, tag: str) -> bool:
    """``PostMessage(hwnd, WM_CLOSE)`` — True on success, False if it raised.

    ``tag`` is only for the log line (a sid prefix, or ``"orphan"``).
    """
    try:
        win32gui.PostMessage(hwnd, _WM_CLOSE, 0, 0)
        logger.debug(f"PostMessage(WM_CLOSE) → hwnd {hwnd} for {tag}")
        return True
    except Exception as exc:  # noqa: BLE001 — pywintypes.error et al.
        logger.warning(
            f"🪟 PostMessage(WM_CLOSE) → hwnd {hwnd} for {tag} failed: {exc}"
        )
        return False


def close_mirror_window(sid: str) -> bool:
    """Dismiss the PC mirror window for ``sid`` via ``WM_CLOSE``.

    Tries the HWND stashed at spawn time first; when none is stashed or
    it's already dead, falls back to a fresh ``EnumWindows`` title-scan
    for ``app-launcher-mirror-<sid>`` (issue #199). The close-time scan is
    what makes Stop & Close survive a webapp restart — the in-memory
    ``_mirror_hwnds`` registry is wiped on every restart, and a lost
    spawn-time poll race never registers an HWND at all. Returns ``True``
    when a ``WM_CLOSE`` was posted to some window.

    The registry entry (if any) is always dropped so a stale HWND can't
    be retried.
    """
    hwnd = _mirror_hwnds.pop(sid, None)
    try:
        import win32gui  # type: ignore
    except ImportError:
        logger.warning(
            "pywin32 not available — mirror window close skipped for "
            f"sid {sid[:8]}"
        )
        return False
    if hwnd is not None and _post_wm_close(win32gui, hwnd, sid[:8]):
        return True
    # No registered HWND, or PostMessage to it failed (restart wiped the
    # registry / window re-created / poll race lost). Scan live windows by
    # title and close a match if one is still on the desktop.
    scanned = _find_mirror_hwnd(win32gui, _MIRROR_TITLE_PREFIX + sid)
    if scanned is not None:
        logger.debug(
            f"close_mirror_window({sid[:8]}): no usable registered HWND — "
            f"close-time title-scan found hwnd {scanned}"
        )
        return _post_wm_close(win32gui, scanned, sid[:8])
    logger.debug(
        f"close_mirror_window({sid[:8]}): no HWND and no window titled "
        f"'{_MIRROR_TITLE_PREFIX + sid}' on the desktop"
    )
    return False


# SW_RESTORE from winuser.h — un-minimize (and leave a normal window as-is)
# before foregrounding. Inlined like _WM_CLOSE so callers needn't import
# win32con.
_SW_RESTORE = 9


def _is_window(win32gui: Any, hwnd: int) -> bool:
    """``IsWindow(hwnd)`` — False (never raises) if the handle is dead."""
    try:
        return bool(win32gui.IsWindow(hwnd))
    except Exception:  # noqa: BLE001 — pywintypes.error
        return False


def _bring_to_front(win32gui: Any, hwnd: int, tag: str) -> None:
    """Restore (if minimized) and foreground ``hwnd`` — best-effort, never raises.

    Windows forbids ``SetForegroundWindow`` from a background process, so the
    call may only flash the taskbar button rather than raise the window. That
    is acceptable: the point of focusing is to *avoid spawning a duplicate*
    window, not to guarantee a raise — a flashing taskbar button is a fine
    fallback. ``tag`` is a sid prefix for the log line.
    """
    try:
        win32gui.ShowWindow(hwnd, _SW_RESTORE)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"ShowWindow(SW_RESTORE) → hwnd {hwnd} for {tag} skipped: {exc}")
    try:
        win32gui.SetForegroundWindow(hwnd)
        logger.debug(f"SetForegroundWindow → hwnd {hwnd} for {tag}")
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"SetForegroundWindow → hwnd {hwnd} for {tag} failed: {exc}")


def focus_mirror_window(sid: str) -> bool:
    """Foreground ``sid``'s existing mirror window if one is live (issue #282).

    Returns ``True`` when a live mirror window for ``sid`` was found (and a
    best-effort foreground attempted), ``False`` when none exists — in which
    case the caller should spawn a fresh window. A stale registered HWND (its
    window already closed) is re-validated and dropped, then a fresh
    ``EnumWindows`` title-scan re-finds and re-registers the window — so a
    mirror opened before a webapp restart (registry wiped) is still focused,
    not duplicated.
    """
    try:
        import win32gui  # type: ignore
    except ImportError:
        logger.warning(
            "pywin32 not available — mirror window focus skipped for "
            f"sid {sid[:8]}"
        )
        return False
    hwnd = _mirror_hwnds.get(sid)
    if hwnd is not None and not _is_window(win32gui, hwnd):
        forget_mirror_hwnd(sid)
        hwnd = None
    if hwnd is None:
        hwnd = _find_mirror_hwnd(win32gui, _MIRROR_TITLE_PREFIX + sid)
        if hwnd is not None:
            register_mirror_hwnd(sid, hwnd)
    if hwnd is None:
        return False
    _bring_to_front(win32gui, hwnd, sid[:8])
    return True


def open_or_focus_mirror_window(url: str, sid: str) -> str:
    """Focus ``sid``'s existing mirror window if live, else open a fresh one.

    Returns ``"focused"`` or ``"opened"`` (issue #282). This is what makes a
    desktop click on an existing session row behave like the new-session
    launch — a dedicated Edge window — without ever spawning a *second* window
    for the same session: the HWND registry is keyed by sid, so a duplicate
    spawn would orphan the first window's tracked HWND and break Stop & Close.
    """
    if focus_mirror_window(sid):
        return "focused"
    open_local_terminal_window(url, sid)
    return "opened"


def close_orphan_mirror_windows(live_sids: Iterable[str]) -> int:
    """Close every Edge mirror window not backed by a live session (#199).

    Sweeps top-level windows whose title carries the mirror marker
    ``app-launcher-mirror-`` and ``WM_CLOSE``s any whose sid isn't in
    ``live_sids``. This reconciles the orphans the in-memory HWND registry
    can't — it's dropped on every webapp restart, so mirrors opened before
    a restart would otherwise pile up forever. Returns the count closed.

    Membership is tested by marker substring (``app-launcher-mirror-<sid>``
    in the title) rather than parsing the sid out, so an Edge-wrapped title
    can't make a live window look orphaned. Callers must pass a *reliable*
    live-session list — an empty list because the session-host was
    unreachable would sweep every mirror, including live ones.
    """
    try:
        import win32gui  # type: ignore
    except ImportError:
        return 0
    live_markers = [_MIRROR_TITLE_PREFIX + s for s in live_sids]
    orphans: List[int] = []

    def _cb(hwnd: int, _extra: Any) -> bool:
        try:
            title = win32gui.GetWindowText(hwnd) or ""
        except Exception:  # noqa: BLE001
            return True
        if _MIRROR_TITLE_PREFIX in title and not any(
            marker in title for marker in live_markers
        ):
            orphans.append(hwnd)
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception as exc:  # noqa: BLE001 — pywintypes.error
        logger.debug(f"orphan mirror sweep EnumWindows failed: {exc}")
        return 0
    closed = sum(_post_wm_close(win32gui, hwnd, "orphan") for hwnd in orphans)
    if closed:
        logger.info(f"🧹 closed {closed} orphaned mirror window(s)")
    return closed


def _find_mirror_hwnd(
    win32gui: Any, target: str, title_sink: Optional[List[str]] = None
) -> Optional[int]:
    """One ``EnumWindows`` sweep → HWND of the first top-level window whose
    title contains ``target``, else ``None``.

    Substring (not exact) match because some browser modes (PWA, certain
    Edge channels) prepend the app name to the page title. When
    ``title_sink`` is given, every non-empty title seen is appended to it
    for timeout diagnostics.
    """
    found: List[int] = []

    def _cb(hwnd: int, _extra: Any) -> bool:
        if found:
            return False
        try:
            title = win32gui.GetWindowText(hwnd) or ""
        except Exception:  # noqa: BLE001
            return True
        if title and title_sink is not None:
            title_sink.append(title)
        if target in title:
            found.append(hwnd)
            return False
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception as exc:  # noqa: BLE001 — pywintypes.error
        logger.debug(f"EnumWindows failed: {exc}")
    return found[0] if found else None


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
    last_titles: List[str] = []

    while time.monotonic() < deadline:
        attempts += 1
        # Fresh sink each sweep so the timeout diagnostic stays bounded.
        last_titles = []
        hwnd = _find_mirror_hwnd(win32gui, target, last_titles)
        if hwnd is not None:
            logger.debug(
                f"found mirror HWND {hwnd} for sid {sid[:8]} "
                f"after {attempts} sweep(s)"
            )
            register_mirror_hwnd(sid, hwnd)
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
