"""Keep the PC from locking for being idle: a 1 px pointer nudge and a keep-awake request.

``prevent_lock`` (Settings, off by default) arms one loop in the canonical
webapp. While it is on, every :data:`NUDGE_INTERVAL_S` the loop moves the
pointer one pixel through the repo's one ``SendInput`` seam
(:mod:`src.desktop.input`), up and then down in turn so the pointer never
drifts, and holds a keep-awake request so the display and the system stay on.

* **Only moves, never clicks,** and never presses a key.
* **Never fights the user.** Windows reports one idle clock for all input,
  our own included, so a beat is skipped only when that clock was moved by
  something other than the previous nudge: the tick read right after a nudge is
  remembered, and any later tick that differs is real input. A skipped beat
  waits a full interval and does not flip the direction.
* **The keep-awake request is held by one thread.** ``SetThreadExecutionState``
  belongs to the calling thread, so :class:`ExecutionStateHold` parks a thread
  that sets the flags and waits; releasing, or the process ending, clears them.
  Nothing here touches the power plan, the screen-saver policy or any other
  machine setting.
* **Off holds nothing.** With the switch off no request is held and no input
  is sent; turning it off releases at once (:func:`poke` wakes the loop).

The injector, the idle probe, the keep-awake hold and the clock are all
parameters, so the unit tests run on fakes and never move a real pointer.
"""

from __future__ import annotations

import asyncio
import atexit
import ctypes
import logging
import sys
import threading
import time
from typing import Any, Callable, Optional, Protocol, Tuple

from src.desktop import input as desktop_input
from src.desktop.input import Injector, MouseEvent
from src.request_timing import TimedExecutor

logger = logging.getLogger(__name__)

# Well under the shortest idle-lock timeout a person sets, and slow enough that
# the loop is invisible in the request budget.
NUDGE_INTERVAL_S = 15.0

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
HOLD_FLAGS = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED

# The thread that sets the flags answers in microseconds; this bounds a wedge.
HOLD_WAIT_S = 5.0

# (tick of the last input, tick now), both GetTickCount milliseconds; None when
# Windows would not say.
IdleProbe = Callable[[], Optional[Tuple[int, int]]]

# The tick clock is 32 bits and wraps every 49 days.
_TICK_MASK = 0xFFFFFFFF

# The loop's own thread: a held request or a slow SendInput stalls only this
# tick, never a request on the loop's shared default pool.
_TICK_POOL = TimedExecutor("prevent-lock-tick", max_workers=1)


class KeepAwake(Protocol):
    def acquire(self) -> bool:
        """Start holding the request; True when it is held."""

    def release(self) -> None:
        """Stop holding it; a no-op when nothing is held."""


def _kernel32() -> Any:
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _set_thread_execution_state(flags: int) -> int:
    """``SetThreadExecutionState`` on the calling thread; 0 when it failed."""
    if sys.platform != "win32":
        return 0
    fn = _kernel32().SetThreadExecutionState
    fn.argtypes = [ctypes.c_uint32]
    fn.restype = ctypes.c_uint32
    return int(fn(flags))


class ExecutionStateHold:
    """A per-thread keep-awake request, kept alive by a parked thread."""

    def __init__(self, set_state: Callable[[int], int] = _set_thread_execution_state) -> None:
        self._set_state = set_state
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def acquire(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            stop = self._stop = threading.Event()
            ready = threading.Event()
            held = []

            def park() -> None:
                ok = self._set_state(HOLD_FLAGS) != 0
                held.append(ok)
                ready.set()
                if ok:
                    stop.wait()
                    self._set_state(ES_CONTINUOUS)

            thread = threading.Thread(target=park, name="prevent-lock-hold", daemon=True)
            thread.start()
            if not ready.wait(HOLD_WAIT_S) or not held[0]:
                stop.set()
                return False
            self._thread = thread
            return True

    def release(self) -> None:
        with self._lock:
            thread, self._thread = self._thread, None
            self._stop.set()
        if thread is not None:
            thread.join(HOLD_WAIT_S)


def system_idle_probe() -> Optional[Tuple[int, int]]:
    """``GetLastInputInfo`` and ``GetTickCount``; None off Windows or on failure."""
    if sys.platform != "win32":
        return None
    from ctypes import wintypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    return int(info.dwTime), int(_kernel32().GetTickCount()) & _TICK_MASK


class PreventLock:
    """The switch's state machine: one :meth:`step` per beat, on any thread
    that does not run two at once."""

    def __init__(
        self,
        injector: Optional[Injector] = None,
        idle_probe: IdleProbe = system_idle_probe,
        keep_awake: Optional[KeepAwake] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._injector = injector
        self._idle_probe = idle_probe
        self._keep_awake: KeepAwake = keep_awake if keep_awake is not None else ExecutionStateHold()
        self._clock = clock
        self._lock = threading.Lock()
        self._reset()

    def _reset(self) -> None:
        self._on = False
        self._due = 0.0
        self._dy = -1
        self._own_tick: Optional[int] = None
        self._failing = False

    def step(self, enabled: bool) -> str:
        """Apply the switch and, when a beat is due, nudge. Returns what
        happened: ``off``, ``waiting``, ``skipped`` (real input), ``moved`` or
        ``failed`` (Windows took no event)."""
        with self._lock:
            if not enabled:
                return self._turn_off()
            if not self._on:
                self._turn_on()
            now = self._clock()
            if now < self._due:
                return "waiting"
            self._due = now + NUDGE_INTERVAL_S
            return self._beat()

    def shutdown(self) -> None:
        """Release the request whatever the switch says (loop end, process exit)."""
        with self._lock:
            self._turn_off()

    def _turn_on(self) -> None:
        self._on = True
        if self._keep_awake.acquire():
            logger.info("ℹ️ prevent lock: on, keep-awake request held, nudging every %ds", NUDGE_INTERVAL_S)
        else:
            logger.warning("⚠️ prevent lock: on, but Windows refused the keep-awake request; the nudge still runs")

    def _turn_off(self) -> str:
        if self._on:
            self._keep_awake.release()
            logger.info("ℹ️ prevent lock: off, keep-awake request released")
        self._reset()
        return "off"

    def _recent_real_input(self) -> bool:
        probe = self._idle_probe()
        if probe is None:
            return False
        last, now = probe
        idle_ms = (now - last) & _TICK_MASK
        return last != self._own_tick and idle_ms < NUDGE_INTERVAL_S * 1000

    def _beat(self) -> str:
        if self._recent_real_input():
            return "skipped"
        injector = self._injector if self._injector is not None else desktop_input.get_injector()
        accepted = injector.send([MouseEvent(desktop_input.MOUSEEVENTF_MOVE, dx=0, dy=self._dy)])
        if accepted < 1:
            if not self._failing:
                logger.warning("⚠️ prevent lock: Windows accepted no pointer move (a locked or elevated desktop)")
            self._failing = True
            return "failed"
        self._failing = False
        self._dy = -self._dy
        probe = self._idle_probe()
        self._own_tick = probe[0] if probe is not None else None
        return "moved"


_controller = PreventLock()
# A process ending without the loop's cleanup (a hard exit path) still drops the request.
atexit.register(_controller.shutdown)
_wake: Optional[asyncio.Event] = None


def poke() -> None:
    """Wake the loop now, so a Settings change applies without waiting a beat.
    Called on the event loop (the config route)."""
    if _wake is not None:
        _wake.set()


async def tick_forever(app: Any, controller: Optional[PreventLock] = None) -> None:
    """The lifespan loop; the switch is re-read every beat and on every poke.
    Always started and idle while the switch is off. Canonical instance only,
    so a disposable gate webapp never moves the real pointer."""
    global _wake
    ctl = controller if controller is not None else _controller
    _wake = wake = asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        while True:
            cfg = getattr(app.state, "webapp_config", None)
            enabled = bool(getattr(cfg, "prevent_lock", False))
            try:
                await loop.run_in_executor(_TICK_POOL, ctl.step, enabled)
            except Exception:  # noqa: BLE001 — never take the lifespan down
                logger.exception("❌ prevent lock beat failed")
            try:
                await asyncio.wait_for(wake.wait(), NUDGE_INTERVAL_S)
            except asyncio.TimeoutError:
                pass
            wake.clear()
    finally:
        _wake = None
        ctl.shutdown()
