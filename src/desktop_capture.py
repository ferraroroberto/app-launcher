"""Screen view (#1520): capture one monitor of the PC's desktop for the phone.

The webapp is the capturing process. It runs in the user's interactive
session under the tray, so plain GDI reaches the desktop with no admin
rights and no extra app. Nothing here belongs to the session-host.

* **Physical pixels everywhere.** :func:`ensure_dpi_awareness` makes the
  process per-monitor-v2 DPI aware before the first monitor read or grab, so
  monitor rects and captured sizes agree on a scaled display (step 2/2, input,
  depends on it). Every public entry point calls it; it runs once.
* **One monitor, not the virtual desktop.** Pillow's ``ImageGrab.grab`` always
  grabs the primary or the whole virtual desktop and *then* crops to a bbox,
  so a per-monitor bbox costs the full 3840x2160 grab. :func:`grab_monitor`
  BitBlts just the monitor's rect out of the screen DC instead. Rects are in
  virtual-desktop coordinates, so a monitor above or left of the primary has
  a negative origin, and BitBlt takes that as is.
* **Capture runs only while a viewer is connected.** There is no background
  task: :func:`stream_frames` is driven by the WebSocket handler and returns
  when the viewer goes away.
* **Unchanged frames are not sent.** A digest of a small thumbnail decides;
  an idle screen sends one frame and then nothing.
* **Client-acked flow control.** The next frame is grabbed only after the
  client acks the last one, so a slow link never builds a queue and what the
  phone sees is always the latest screen, not a backlog.

A grab can fail: the workstation is locked (the input desktop is Winlogon's
secure desktop, which a user-session process cannot read), or the session is
disconnected. That is :class:`CaptureUnavailable`, its own state that the
viewer shows as such, never a black or frozen frame passed off as live.
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import io
import json
import logging
import sys
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from PIL import Image

logger = logging.getLogger(__name__)

# Pacing: at most one grab per interval while frames are flowing (~7 fps),
# under the issue's 5-10 fps target and well inside its ~1 s change budget.
FRAME_INTERVAL_S = 0.15
# While the desktop can't be read (locked, disconnected), retry this often.
UNAVAILABLE_RETRY_S = 1.0
# The width the client asks for is clamped to this range; a frame is never
# upscaled past the monitor's own width.
MIN_WIDTH = 320
MAX_WIDTH = 3840
DEFAULT_WIDTH = 1280
JPEG_QUALITY = 70
# Thumbnail for change detection: a 4x box reduction keeps a one-character
# change (a clock tick) visible in the digest at a fraction of the bytes.
THUMB_REDUCE = 4
# Quality presets (#1721). The connect query's ``preset=`` is validated against
# PRESETS: JPEG quality, the pause between frames, and a cap on the frame width
# on top of what the phone asks for (None = no extra cap). Balanced is built
# from the constants above, so it can never drift from "today".
DEFAULT_PRESET = "balanced"
SAVER_WIDTH = 960
# How often the per-stage times go out while the readout is on.
STATS_INTERVAL_S = 1.0
# The client's readout switch, the one non-input control message on the socket.
READOUT_MESSAGE = "readout"

# DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 is the pseudo-handle -4.
_PER_MONITOR_AWARE_V2 = -4
_MONITORINFOF_PRIMARY = 0x1

_dpi_lock = threading.Lock()
_dpi_done = False


class CaptureUnavailable(Exception):
    """The desktop can't be read right now (locked, disconnected, no display)."""


@dataclass(frozen=True)
class Monitor:
    """One display, in physical pixels on the virtual desktop."""

    id: int
    left: int
    top: int
    width: int
    height: int
    primary: bool

    @property
    def rect(self) -> Tuple[int, int, int, int]:
        return (self.left, self.top, self.left + self.width, self.top + self.height)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Preset:
    """One quality preset: what the encoder and the pacing use."""

    name: str
    quality: int
    interval: float
    max_width: Optional[int]


PRESETS: Dict[str, Preset] = {
    "sharp": Preset("sharp", 85, FRAME_INTERVAL_S, None),
    DEFAULT_PRESET: Preset(DEFAULT_PRESET, JPEG_QUALITY, FRAME_INTERVAL_S, None),
    "saver": Preset("saver", 50, 0.3, SAVER_WIDTH),
}


def pick_preset(name: Optional[str]) -> Preset:
    """The preset called ``name``; anything unknown is Balanced."""
    return PRESETS.get((name or "").strip().lower(), PRESETS[DEFAULT_PRESET])


class StageStats:
    """How long each stage of the capture loop took, averaged between two
    ``take()`` calls: grab, digest, resize, encode.

    ``take`` returns the period's means in milliseconds as a ``stats`` message
    (None for a stage that did not run in the period, e.g. encode on a still
    screen) and starts a new period. ``record`` runs on the capture loop's
    worker threads and ``take`` on the event loop, with no lock: a ``take``
    racing a ``record`` can drop one sample, which a mean over a second
    absorbs.
    """

    STAGES = ("grab", "digest", "resize", "encode")

    def __init__(self) -> None:
        self._sums: Dict[str, float] = {stage: 0.0 for stage in self.STAGES}
        self._counts: Dict[str, int] = {stage: 0 for stage in self.STAGES}

    def record(self, stage: str, seconds: float) -> None:
        self._sums[stage] += seconds
        self._counts[stage] += 1

    def take(self) -> Optional[Dict[str, Any]]:
        """The period's means, or None when no stage ran."""
        sums, counts = self._sums, self._counts
        self._sums = {stage: 0.0 for stage in self.STAGES}
        self._counts = {stage: 0 for stage in self.STAGES}
        if not any(counts.values()):
            return None
        message: Dict[str, Any] = {"type": "stats"}
        for stage in self.STAGES:
            message[stage] = (
                round(sums[stage] / counts[stage] * 1000, 1) if counts[stage] else None
            )
        message["frames"] = counts["encode"]
        return message


def parse_readout(text: str) -> Optional[bool]:
    """The readout switch in a client text frame, or None if it is not one.

    ``{"type": "readout", "on": true}``: only a literal ``true`` turns the
    readout on. Kept out of the input path on purpose: it is not input and is
    not counted as input.
    """
    if len(text) > 64 or READOUT_MESSAGE not in text:
        return None
    try:
        message = json.loads(text)
    except ValueError:
        return None
    if not isinstance(message, dict) or message.get("type") != READOUT_MESSAGE:
        return None
    return message.get("on") is True


def ensure_dpi_awareness() -> None:
    """Make this process per-monitor-v2 DPI aware, once, before any capture.

    Without it a scaled display reports logical sizes while BitBlt copies
    physical pixels, so rects and frames disagree. Setting it fails when the
    process already has an awareness (a manifest, an earlier call); that is
    logged, not raised, since the capture still works at whatever awareness
    the process has.
    """
    global _dpi_done
    if _dpi_done or sys.platform != "win32":
        return
    with _dpi_lock:
        if _dpi_done:
            return
        _dpi_done = True
        try:
            ok = ctypes.windll.user32.SetProcessDpiAwarenessContext(
                ctypes.c_void_p(_PER_MONITOR_AWARE_V2)
            )
        except (AttributeError, OSError) as exc:
            logger.warning("⚠️ Screen: could not set DPI awareness: %s", exc)
            return
        if ok:
            logger.info("ℹ️ Screen: process set per-monitor-v2 DPI aware")
        else:
            logger.info(
                "ℹ️ Screen: DPI awareness already set for this process "
                "(SetProcessDpiAwarenessContext error %s); capturing as is",
                ctypes.GetLastError(),
            )


def list_monitors() -> List[Monitor]:
    """Every attached monitor, in the system's enumeration order.

    An empty list means none could be read (not Windows, no display). The id
    is the position in that order, stable while the display layout is.
    """
    if sys.platform != "win32":
        return []
    ensure_dpi_awareness()
    import pywintypes
    import win32api

    monitors: List[Monitor] = []
    try:
        handles = win32api.EnumDisplayMonitors()
    except pywintypes.error as exc:
        logger.warning("⚠️ Screen: EnumDisplayMonitors failed: %s", exc)
        return []
    for index, (handle, _dc, _rect) in enumerate(handles):
        try:
            info = win32api.GetMonitorInfo(handle)
        except pywintypes.error as exc:
            logger.warning("⚠️ Screen: GetMonitorInfo failed for #%d: %s", index, exc)
            continue
        left, top, right, bottom = info["Monitor"]
        monitors.append(
            Monitor(
                id=index,
                left=left,
                top=top,
                width=right - left,
                height=bottom - top,
                primary=bool(info.get("Flags", 0) & _MONITORINFOF_PRIMARY),
            )
        )
    return monitors


def pick_monitor(monitors: List[Monitor], wanted: Optional[int]) -> Optional[Monitor]:
    """The monitor with id ``wanted``, or the primary when none was asked for."""
    if wanted is None:
        return next((m for m in monitors if m.primary), monitors[0] if monitors else None)
    return next((m for m in monitors if m.id == wanted), None)


def grab_monitor(monitor: Monitor) -> Image.Image:
    """BitBlt one monitor's rect out of the screen DC into an RGB image.

    Raises :class:`CaptureUnavailable` when the desktop can't be read.
    """
    if sys.platform != "win32":
        raise CaptureUnavailable("screen capture needs Windows")
    ensure_dpi_awareness()
    import pywintypes
    import win32con
    import win32gui
    import win32ui

    width, height = monitor.width, monitor.height
    screen_dc = mem_dc = bitmap = None
    window_dc = 0
    try:
        window_dc = win32gui.GetWindowDC(0)
        screen_dc = win32ui.CreateDCFromHandle(window_dc)
        mem_dc = screen_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(screen_dc, width, height)
        mem_dc.SelectObject(bitmap)
        mem_dc.BitBlt(
            (0, 0), (width, height), screen_dc, (monitor.left, monitor.top),
            win32con.SRCCOPY,
        )
        bits = bitmap.GetBitmapBits(True)
    except (win32ui.error, pywintypes.error) as exc:
        raise CaptureUnavailable(str(exc)) from exc
    finally:
        if mem_dc is not None:
            mem_dc.DeleteDC()
        if screen_dc is not None:
            screen_dc.DeleteDC()
        if window_dc:
            win32gui.ReleaseDC(0, window_dc)
        if bitmap is not None:
            win32gui.DeleteObject(bitmap.GetHandle())
    return bgrx_to_image(bits, width, height)


def bgrx_to_image(bits: bytes, width: int, height: int) -> Image.Image:
    """A top-down 32-bit BGRX buffer (what GetBitmapBits returns) as RGB."""
    return Image.frombuffer("RGB", (width, height), bits, "raw", "BGRX", 0, 1)


def clamp_width(requested: Optional[int]) -> int:
    if not requested:
        return DEFAULT_WIDTH
    return max(MIN_WIDTH, min(MAX_WIDTH, int(requested)))


def frame_width(requested: Optional[int], monitor_width: int, preset: Preset) -> int:
    """The width frames go out at: what the phone asked for (clamped), never
    past the monitor's own width, and never past the preset's cap."""
    width = min(clamp_width(requested), monitor_width)
    return min(width, preset.max_width) if preset.max_width else width


def thumbnail_digest(image: Image.Image) -> bytes:
    """A cheap fingerprint of what is on screen, for skipping unchanged frames."""
    return hashlib.blake2b(image.reduce(THUMB_REDUCE).tobytes(), digest_size=16).digest()


def resize_to_width(image: Image.Image, width: int) -> Image.Image:
    """Downscale to ``width`` (never up)."""
    if image.width > width:
        height = max(1, round(image.height * width / image.width))
        return image.resize((width, height), Image.BILINEAR)
    return image


def jpeg_bytes(image: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


async def _wait_or_stop(event: asyncio.Event, stopped: asyncio.Event) -> bool:
    """Wait for ``event`` or ``stopped``; True when ``event`` won."""
    if event.is_set():
        return not stopped.is_set()
    waiters = {asyncio.ensure_future(event.wait()), asyncio.ensure_future(stopped.wait())}
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
    return event.is_set() and not stopped.is_set()


async def _sleep_or_stop(stopped: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stopped.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def stream_frames(
    grab: Callable[[], Image.Image],
    send_frame: Callable[[bytes], Awaitable[None]],
    send_status: Callable[[str], Awaitable[None]],
    acked: asyncio.Event,
    stopped: asyncio.Event,
    *,
    width: int,
    interval: float = FRAME_INTERVAL_S,
    retry: float = UNAVAILABLE_RETRY_S,
    quality: int = JPEG_QUALITY,
    stats: Optional[StageStats] = None,
) -> int:
    """Grab, compare and send frames until ``stopped`` is set; return frames sent.

    ``acked`` is set by the caller when the client acks a frame (and once up
    front, so the first frame goes at once). A frame is grabbed only while it
    is set, so at most one frame is ever in flight. ``send_status`` reports
    ``"unavailable"`` once when the desktop stops being readable and
    ``"live"`` once when it comes back. ``quality`` and ``interval`` come from
    the viewer's preset (#1721); ``stats``, when given, collects how long each
    stage took (grab, digest, resize, encode) for the readout.
    """

    def grab_and_digest() -> Tuple[Image.Image, bytes]:
        began = time.perf_counter()
        image = grab()
        grabbed = time.perf_counter()
        digest = thumbnail_digest(image)
        if stats is not None:
            stats.record("grab", grabbed - began)
            stats.record("digest", time.perf_counter() - grabbed)
        return image, digest

    def resize_and_encode(image: Image.Image) -> bytes:
        began = time.perf_counter()
        resized = resize_to_width(image, width)
        resized_at = time.perf_counter()
        data = jpeg_bytes(resized, quality)
        if stats is not None:
            stats.record("resize", resized_at - began)
            stats.record("encode", time.perf_counter() - resized_at)
        return data

    frames = 0
    last_digest: Optional[bytes] = None
    unavailable = False
    while not stopped.is_set():
        if not await _wait_or_stop(acked, stopped):
            break
        try:
            image, digest = await asyncio.to_thread(grab_and_digest)
        except CaptureUnavailable as exc:
            if not unavailable:
                unavailable = True
                logger.info("ℹ️ Screen: desktop unreadable, retrying: %s", exc)
                await send_status("unavailable")
            await _sleep_or_stop(stopped, retry)
            continue
        if unavailable:
            unavailable = False
            last_digest = None
            await send_status("live")
        if digest != last_digest:
            data = await asyncio.to_thread(resize_and_encode, image)
            if stopped.is_set():
                break
            acked.clear()
            await send_frame(data)
            frames += 1
            last_digest = digest
        await _sleep_or_stop(stopped, interval)
    return frames
