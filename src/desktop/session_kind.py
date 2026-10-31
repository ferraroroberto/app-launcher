"""Is the PC's interactive session a Remote Desktop session (#1996).

A Remote Desktop session has no microphone of its own: dictation in the PC's
own browser only works when the RDP client redirects its mic, so a failed
``getUserMedia`` there deserves a specific hint instead of the generic toast.

**A live read, never cached.** The session name follows the user: it is
``Console`` at the keyboard and ``RDP-Tcp#<n>`` once they reconnect over
Remote Desktop, with this process still running in the same session id. One
``ProcessIdToSessionId`` plus ``WTSQuerySessionInformationW`` is microseconds,
so each ask re-reads it.

**Unknown is its own state.** A non-Windows host, a failed call or a name that
is neither ``Console`` nor ``RDP-Tcp#...`` (a service session, a virtual
display) is ``unknown``, which the page treats like a local session: the
generic message, never a guess.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Literal

logger = logging.getLogger(__name__)

SessionState = Literal["remote", "local", "unknown"]

_WTS_CURRENT_SERVER_HANDLE = 0
# WTS_INFO_CLASS.WTSWinStationName
_WTS_WIN_STATION_NAME = 6


def _station_name() -> str:
    """This process's window-station name (``Console``, ``RDP-Tcp#0``...).

    Raises ``OSError`` when Windows will not say.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)

    session_id = wintypes.DWORD()
    if not kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session_id)):
        raise ctypes.WinError(ctypes.get_last_error())

    buf = ctypes.c_void_p()
    size = wintypes.DWORD()
    query = wtsapi32.WTSQuerySessionInformationW
    query.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    if not query(
        _WTS_CURRENT_SERVER_HANDLE,
        session_id.value,
        _WTS_WIN_STATION_NAME,
        ctypes.byref(buf),
        ctypes.byref(size),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.wstring_at(buf.value) if buf.value else ""
    finally:
        wtsapi32.WTSFreeMemory(buf)


def classify(station_name: str) -> SessionState:
    """The state a window-station name stands for."""
    name = station_name.strip().lower()
    if name.startswith("rdp-tcp#"):
        return "remote"
    if name == "console":
        return "local"
    return "unknown"


def read() -> SessionState:
    """The session's state now; ``unknown`` whenever it cannot be established."""
    if sys.platform != "win32":
        return "unknown"
    try:
        return classify(_station_name())
    except OSError as exc:
        logger.warning("⚠️ session name unreadable: %s", exc)
        return "unknown"
