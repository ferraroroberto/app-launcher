"""Read from / signal another process's console (Windows only).

``AttachConsole`` is process-global and the webapp is multithreaded, so
the attach must NOT happen inside the webapp process — it would hijack
the server's own console state. Instead the public helpers shell out to
a fresh, short-lived ``python -m src.console_ctrl …`` subprocess that
detaches any console, attaches to the target's, does its one job, and
detaches again.

- :func:`send_ctrl_c` raises ``CTRL_C_EVENT`` in the target's console —
  Claude Code's own interrupt/exit path, far less fragile than
  synthesising literal ``/quit`` keystrokes.
- :func:`get_console_title` reads the target console's title bar, which
  Claude Code keeps set to the current task summary — the only way to
  tell two sessions in the same repo apart.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# wincon.h: CTRL_C_EVENT. GenerateConsoleCtrlEvent only delivers
# CTRL_C_EVENT to the whole console (group id must be 0).
_CTRL_C_EVENT = 0

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def send_ctrl_c(pid: int, timeout: float = 5.0) -> bool:
    """Deliver a Ctrl+C to the console hosting ``pid``.

    Returns ``True`` when the helper attached and raised the event,
    ``False`` on any failure (not Windows, no such console, denied).
    """
    if sys.platform != "win32":
        return False
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "src.console_ctrl", str(pid)],
            capture_output=True,
            timeout=timeout,
            cwd=str(_PROJECT_ROOT),
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug(f"send_ctrl_c({pid}) helper failed: {exc}")
        return False
    if result.returncode != 0:
        logger.debug(f"send_ctrl_c({pid}) helper exited {result.returncode}")
    return result.returncode == 0


def get_console_title(pid: int, timeout: float = 4.0) -> Optional[str]:
    """Return the title of the console hosting ``pid``, or ``None``.

    Claude Code keeps the console title set to the current task
    summary, so this is what distinguishes two sessions in one repo.
    """
    if sys.platform != "win32":
        return None
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "src.console_ctrl", "--title", str(pid)],
            capture_output=True,
            timeout=timeout,
            cwd=str(_PROJECT_ROOT),
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug(f"get_console_title({pid}) helper failed: {exc}")
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.decode("utf-8", errors="replace").strip()
    # Claude Code prefixes the title with a decorative status glyph that
    # doesn't reliably survive the UTF-16 → pipe round trip. Drop leading
    # non-alphanumerics (glyph, nbsp, decode garbage) so the task summary
    # — the part that actually distinguishes two sessions — comes through
    # clean.
    idx = 0
    while idx < len(raw) and not raw[idx].isalnum():
        idx += 1
    cleaned = raw[idx:].strip()
    return cleaned or None


def _attach_and_signal(pid: int) -> int:
    """Attach to ``pid``'s console and raise Ctrl+C. Helper-process only."""
    import ctypes

    kernel32 = ctypes.windll.kernel32
    # Drop whatever console we might have, then borrow the target's.
    kernel32.FreeConsole()
    if not kernel32.AttachConsole(pid):
        return 1
    # Ignore the Ctrl+C in *this* helper so it doesn't take itself down
    # before the event is delivered to the target.
    kernel32.SetConsoleCtrlHandler(None, True)
    ok = kernel32.GenerateConsoleCtrlEvent(_CTRL_C_EVENT, 0)
    kernel32.FreeConsole()
    return 0 if ok else 2


def _read_console_title(pid: int) -> int:
    """Attach to ``pid``'s console, write its title to stdout. Helper only."""
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.FreeConsole()
    if not kernel32.AttachConsole(pid):
        return 1
    buf = ctypes.create_unicode_buffer(4096)
    kernel32.GetConsoleTitleW(buf, 4096)
    kernel32.FreeConsole()
    # Write UTF-8 bytes directly — the console glyphs Claude Code uses
    # would choke a cp1252 stdout. errors="replace" keeps a stray
    # surrogate from the status glyph from crashing the helper.
    sys.stdout.buffer.write(buf.value.encode("utf-8", errors="replace"))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    _args = sys.argv[1:]
    try:
        if len(_args) == 2 and _args[0] == "--title":
            sys.exit(_read_console_title(int(_args[1])))
        if len(_args) == 1:
            sys.exit(_attach_and_signal(int(_args[0])))
    except ValueError:
        sys.exit(64)
    sys.exit(64)
