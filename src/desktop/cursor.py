"""Screen view (#1598): where the PC's mouse cursor is, and what shape it has.

The capture is a plain BitBlt, so the cursor is never in the picture, and a
cursor-only move changes no pixel the frame digest sees. The phone draws the
cursor itself from small JSON messages on the same socket instead:

    {"type": "cursor", "x": 0.4213, "y": 0.0871, "shape": "text", "visible": true}
    {"type": "cursor", "visible": false}

* **Shape names, never a bitmap.** A cursor image can carry an application's
  content, so the handle ``GetCursorInfo`` reports is compared with the
  system's standard cursors (``LoadCursorW(NULL, IDC_*)``, shared handles
  that stay put for the session) and only the matching name is sent. Any
  other cursor is ``arrow``.
* **Fractions of the shown monitor**, the inverse of the input side's
  ``map_point``: the cursor pixel's centre as 0-1. Off that monitor, or not
  showing, it is hidden.
* **Physical pixels.** The process is per-monitor-v2 DPI aware
  (:func:`src.desktop.capture.ensure_dpi_awareness`), so the cursor position
  and the monitor rects are on the same scale.
* **Sent on change only**, polled about 30 times a second while a viewer is
  connected, and not paced by the frame ack: the cursor moves while the
  picture is still. :func:`stream_cursor` stops with the socket.
* **Read on the event loop.** ``GetCursorInfo`` reads shared user32 state and
  never waits on another process, so a poll this frequent stays off the
  default executor the slow transcript reads contend for (#1605).

While the cursor can't be read (the locked workstation's secure desktop),
it is sent hidden once, logged once, and retried at the capture loop's slow
pace, never in a tight loop.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import sys
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Tuple

from src.desktop import capture as desktop_capture
from src.desktop.capture import Monitor

logger = logging.getLogger(__name__)

# ~30 polls a second: smooth enough to follow a hand on the mouse, and each
# poll is one user32 read.
CURSOR_INTERVAL_S = 1 / 30
# While the cursor can't be read, retry as often as the frame loop does.
UNAVAILABLE_RETRY_S = desktop_capture.UNAVAILABLE_RETRY_S

DEFAULT_SHAPE = "arrow"
# The standard cursors by IDC_* resource id, and the name the phone draws.
# IDC_APPSTARTING (the arrow with a small wheel) reads as busy too.
STANDARD_SHAPES: Tuple[Tuple[int, str], ...] = (
    (32512, "arrow"),        # IDC_ARROW
    (32513, "text"),         # IDC_IBEAM
    (32514, "busy"),         # IDC_WAIT
    (32650, "busy"),         # IDC_APPSTARTING
    (32649, "hand"),         # IDC_HAND
    (32642, "resize-nwse"),  # IDC_SIZENWSE
    (32643, "resize-nesw"),  # IDC_SIZENESW
    (32644, "resize-ew"),    # IDC_SIZEWE
    (32645, "resize-ns"),    # IDC_SIZENS
    (32646, "move"),         # IDC_SIZEALL
    (32648, "not-allowed"),  # IDC_NO
)
SHAPES = frozenset(name for _idc, name in STANDARD_SHAPES)

_CURSOR_SHOWING = 0x1


class CursorUnavailable(Exception):
    """The cursor can't be read right now (secure desktop, no session)."""


@dataclass(frozen=True)
class CursorState:
    """The cursor as the phone draws it: 0-1 in the shown monitor."""

    visible: bool
    x: float = 0.0
    y: float = 0.0
    shape: str = DEFAULT_SHAPE

    def to_message(self) -> Dict[str, Any]:
        if not self.visible:
            return {"type": "cursor", "visible": False}
        return {
            "type": "cursor", "x": self.x, "y": self.y,
            "shape": self.shape, "visible": True,
        }


HIDDEN = CursorState(visible=False)


def shape_for(handle: Optional[int], standard: Mapping[int, str]) -> str:
    """The shape name for a cursor handle; anything not standard is an arrow."""
    if not handle:
        return DEFAULT_SHAPE
    return standard.get(handle, DEFAULT_SHAPE)


def cursor_fraction(monitor: Monitor, px: int, py: int) -> Optional[Tuple[float, float]]:
    """Physical pixel ``(px, py)`` on the virtual desktop as 0-1 in ``monitor``
    (the pixel's centre), or ``None`` when it is on another monitor."""
    dx = px - monitor.left
    dy = py - monitor.top
    if not (0 <= dx < monitor.width and 0 <= dy < monitor.height):
        return None
    return (
        round((dx + 0.5) / monitor.width, 4),
        round((dy + 0.5) / monitor.height, 4),
    )


def cursor_state(
    monitor: Monitor, showing: bool, handle: Optional[int], px: int, py: int,
    standard: Mapping[int, str],
) -> CursorState:
    """What to draw for one ``GetCursorInfo`` reading."""
    if not showing:
        return HIDDEN
    at = cursor_fraction(monitor, px, py)
    if at is None:
        return HIDDEN
    return CursorState(visible=True, x=at[0], y=at[1], shape=shape_for(handle, standard))


# ------------------------------------------------------------ the Win32 edge


def _user32() -> Tuple[Any, type]:
    """A private user32 handle and the ``CURSORINFO`` struct. Private, so the
    prototypes set here never change the shared ``ctypes.windll.user32``
    other modules call."""
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)

    class CURSORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("hCursor", ctypes.c_void_p),
            ("ptScreenPos", wintypes.POINT),
        ]

    user32.GetCursorInfo.argtypes = [ctypes.POINTER(CURSORINFO)]
    user32.GetCursorInfo.restype = wintypes.BOOL
    # Handles are pointer-sized; the default int restype would truncate them.
    user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
    user32.LoadCursorW.restype = ctypes.c_void_p
    return user32, CURSORINFO


def standard_cursors(user32: Any) -> Dict[int, str]:
    """The system's standard cursor handles, mapped to their shape names."""
    table: Dict[int, str] = {}
    for idc, name in STANDARD_SHAPES:
        handle = user32.LoadCursorW(None, ctypes.c_void_p(idc))
        if handle:
            table.setdefault(handle, name)
    return table


def cursor_reader(monitor: Monitor) -> Callable[[], CursorState]:
    """A function reading the cursor against ``monitor``; it raises
    :class:`CursorUnavailable` when the cursor can't be read."""
    if sys.platform != "win32":
        def unavailable() -> CursorState:
            raise CursorUnavailable("cursor reading needs Windows")
        return unavailable
    desktop_capture.ensure_dpi_awareness()
    user32, cursor_info = _user32()
    standard = standard_cursors(user32)

    def read() -> CursorState:
        info = cursor_info()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetCursorInfo(ctypes.byref(info)):
            raise CursorUnavailable(f"GetCursorInfo error {ctypes.get_last_error()}")
        return cursor_state(
            monitor, bool(info.flags & _CURSOR_SHOWING), info.hCursor,
            info.ptScreenPos.x, info.ptScreenPos.y, standard,
        )

    return read


# ------------------------------------------------------------------ the loop


async def stream_cursor(
    read: Callable[[], CursorState],
    send: Callable[[Dict[str, Any]], Awaitable[None]],
    stopped: asyncio.Event,
    *,
    interval: float = CURSOR_INTERVAL_S,
    retry: float = UNAVAILABLE_RETRY_S,
) -> int:
    """Poll the cursor and send it whenever it changed, until ``stopped`` is
    set; return the messages sent. The first reading always goes out."""
    sent = 0
    last: Optional[Dict[str, Any]] = None
    unavailable = False
    while not stopped.is_set():
        try:
            message = read().to_message()
        except CursorUnavailable as exc:
            if not unavailable:
                unavailable = True
                logger.info("ℹ️ Screen: cursor unreadable, hiding it: %s", exc)
            message = HIDDEN.to_message()
        else:
            unavailable = False
        if message != last:
            if stopped.is_set():
                break
            await send(message)
            sent += 1
            last = message
        await desktop_capture.sleep_or_stop(stopped, retry if unavailable else interval)
    return sent
