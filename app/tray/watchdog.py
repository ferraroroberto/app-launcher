"""Tray webapp self-heal primitives: retry-with-backoff, health watchdog, breadcrumbs.

CANONICAL, VENDORED VERBATIM from project-scaffolding. Do **not** edit this file
per-app — it is byte-identical across every fleet tray so a fix made once in the
scaffold re-propagates everywhere. Everything app-specific (the probe, the
respawn action, the log path, the toast) is passed in by the caller, never
hardcoded here, which is what keeps the file identical. Full reasoning + the
worked wiring pattern: scaffold ``docs/windows-tray.md`` (gotcha 5) +
the sister apps' tray watchdog wiring.

The tray is the only long-lived process watching the webapp, and it starts that
webapp exactly once. The failure modes that follow from that, all observed in the
field, each get one primitive here:

* **The initial spawn loses a transient race and never retries.** The port is
  not free yet, a cert renewal is in flight, a dependency hub is not up — one
  failed ``manager.start()`` at tray boot leaves the webapp dead for the tray's
  entire lifetime. :func:`retry_with_backoff` turns that single attempt into a
  bounded, escalating retry (a spawn that failed once used to mean days of silent downtime).

* **The webapp dies or wedges later and nothing notices.** :class:`HealthWatchdog`
  is a consecutive-failure monitor with edge-triggered callbacks — it fires once
  per *transition*, not once per failing tick, so a wedge is a single alert
  rather than a toast every minute.

* **A respawn stacks a second copy while the first is still booting.** On a
  slow boot the child is still importing when the next probe fails, so a
  handler that spawns again leaves both alive, and on Windows both can bind the
  port. :class:`RespawnGuard` owns the one child the tray spawned: it refuses a
  respawn while that child is alive inside its startup window, and stops it
  (and its tree) before any later spawn.

* **Neither event is diagnosable afterwards.** A tray launched by ``pythonw``
  has no ``sys.stderr``, so ``logging.basicConfig()`` writes into the void and
  a boot-time traceback is captured nowhere. :class:`BreadcrumbLog` appends
  timestamped lines to a real file so the *next* occurrence is diagnosable from
  disk with no live repro. This is the piece neither the stdlib default logger
  nor a ``stdout=DEVNULL`` subprocess redirection provides.

**Dead vs wedged is the caller's decision, deliberately.** Nothing here kills a
process on its own: the guard stops only the child the tray itself spawned, and
only when the caller asks it to respawn. ``on_wedge`` receives the failure count and the *app* decides,
because "not listening at all" (safe to respawn) and "listening but not answering
``/healthz``" (auto-killing could mask the real fault) warrant different actions.
:meth:`HealthWatchdog.rearm` exists for the respawn branch: a handler
that already acted asks to be re-evaluated next tick instead of waiting forever
for a recovery a genuinely-dead process will never produce on its own.
"""

from __future__ import annotations

import datetime
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from enum import Enum
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)

#: Poll cadence for :meth:`HealthWatchdog.run`.
DEFAULT_INTERVAL_S = 60.0

#: Consecutive failures before ``on_wedge`` fires. At the default cadence this
#: absorbs a normal tray-menu webapp restart (a few seconds of downtime) without
#: a false alarm.
DEFAULT_FAILURES_TO_ALERT = 3

#: Sleep between retries of the *initial* webapp spawn at tray boot. Escalating,
#: bounded, and short enough that a tray that really can't start still gives up
#: inside a minute.
DEFAULT_STARTUP_RETRY_DELAYS_S: tuple[float, ...] = (5.0, 15.0, 30.0)

#: How long a live child the tray spawned is presumed to be still starting. On a
#: slow boot (after an unclean reboot) a webapp can take minutes to import and
#: bind; inside this window a failing probe means "starting", never "spawn
#: another". Past it, a child that still does not answer is stopped first.
DEFAULT_STARTUP_WINDOW_S = 300.0

#: Cap on the startup-window backoff: each child stopped for never getting
#: healthy doubles the next child's window, so a boot slower than the base
#: window still finishes instead of being cut off every time.
_MAX_WINDOW_BACKOFF = 8

#: Bound on the Windows ``taskkill`` call so a hung kill can never hang the tray.
_STOP_TIMEOUT_S = 30.0


def retry_with_backoff(
    fn: Callable[[], object],
    delays: Sequence[float] = DEFAULT_STARTUP_RETRY_DELAYS_S,
    on_attempt_failed: Callable[[int, BaseException], None] | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Call ``fn()`` until it returns normally or every delay is exhausted.

    Sleeps ``delays[i]`` seconds after the ``(i+1)``-th failure before retrying,
    so ``len(delays) + 1`` attempts are made in total. Re-raises the last
    exception once attempts are exhausted — a permanently-failing start must
    stay a loud failure, not a silent one.

    ``on_attempt_failed(attempt_number, exc)`` — 1-indexed — fires after each
    failure, *before* sleeping or re-raising, so the caller can log/toast/
    breadcrumb the attempt it just lost. ``sleep`` is injectable for tests only.
    """
    attempt = 0
    while True:
        try:
            fn()
            return
        except Exception as exc:  # noqa: BLE001 — deliberately generic retry
            attempt += 1
            if on_attempt_failed is not None:
                on_attempt_failed(attempt, exc)
            if attempt > len(delays):
                raise
            sleep(delays[attempt - 1])


class WatchState(Enum):
    """The closed set of modes a :class:`HealthWatchdog` is in."""

    #: No failing probe on the books: the start state, and any passing probe.
    HEALTHY = "healthy"
    #: Failing probes, still below the threshold; no alert has fired.
    FAILING = "failing"
    #: At or past the threshold and alerted: ``on_wedge`` fired and awaits a recovery.
    WEDGED = "wedged"
    #: At or past the threshold, alert already fired, handler called ``rearm()``:
    #: ``on_wedge`` fires again on the next failing probe, and a passing one is a recovery.
    REARMED = "rearmed"


class WatchStep(NamedTuple):
    """What one probe does: the next state and count, and the callbacks to fire."""

    state: WatchState
    failures: int
    wedge: bool
    recover: bool
    failing: bool


def next_watch_state(
    state: WatchState, ok: bool, failures: int, threshold: int
) -> WatchStep:
    """The only code that maps ``(state, probe result)`` to the next state.

    ``failures`` is the consecutive-failure count before this probe. A pair that
    cannot occur (healthy with failures, wedged below the threshold, ...) raises
    ``ValueError`` rather than being read as the nearest legal state.
    """
    if state is WatchState.HEALTHY:
        legal = failures == 0
    elif state is WatchState.FAILING:
        legal = 0 < failures < threshold
    else:
        legal = failures >= threshold
    if not legal:
        raise ValueError(
            f"impossible watchdog state: {state.value} with {failures} consecutive "
            f"failures (threshold {threshold})"
        )

    if ok:
        # Both alerted states owe the owner a recovery: the wedge alert went out.
        alerted = state in (WatchState.WEDGED, WatchState.REARMED)
        return WatchStep(WatchState.HEALTHY, 0, False, alerted, False)

    failures += 1
    if state is WatchState.WEDGED:
        return WatchStep(WatchState.WEDGED, failures, False, False, True)
    if failures >= threshold:
        return WatchStep(WatchState.WEDGED, failures, True, False, True)
    return WatchStep(WatchState.FAILING, failures, False, False, False)


class HealthWatchdog:
    """Consecutive-failure health monitor with edge-triggered callbacks.

    :meth:`tick` runs one probe. After ``failures_to_alert`` *consecutive*
    failures it fires ``on_wedge(count)`` **once** — edge-triggered, not again
    until a recovery (or an explicit :meth:`rearm`) re-arms it — and the first
    success after an alert, re-armed or not, fires ``on_recover()``. The current
    mode is the read-only :attr:`state`.

    The optional ``on_failing(count)`` is level-triggered: it fires on every
    failed probe while the watchdog is alerted (on the alert tick itself, right
    after ``on_wedge``). A recovery policy such as "restart once it has been
    failing past a grace period" lives in that callback, so the watchdog needs
    to know nothing about the grace period or the retry.

    A probe that raises counts as a failure: the tray must never die because a
    health check threw.
    """

    def __init__(
        self,
        probe: Callable[[], bool],
        on_wedge: Callable[[int], None],
        on_recover: Callable[[], None],
        failures_to_alert: int = DEFAULT_FAILURES_TO_ALERT,
        on_failing: Callable[[int], None] | None = None,
    ) -> None:
        self._probe = probe
        self._on_wedge = on_wedge
        self._on_recover = on_recover
        self._on_failing = on_failing
        self._failures_to_alert = failures_to_alert
        self._consecutive_failures = 0
        self._state = WatchState.HEALTHY

    @property
    def state(self) -> WatchState:
        """The current mode, for a tooltip or status route to read."""
        return self._state

    def rearm(self) -> None:
        """Allow ``on_wedge`` to fire again on the next failing tick, without
        waiting for a recovery.

        Call this from inside ``on_wedge`` when the handler already took action
        (e.g. attempted a respawn) and wants another shot next interval if that
        action didn't fix things. Without it, a handler that respawns and fails
        stays silently alerted forever — a dead process never produces the
        recovery that would otherwise re-arm the edge. A recovery after a
        re-arm still fires ``on_recover``.
        """
        if self._state is WatchState.WEDGED:
            self._state = WatchState.REARMED

    def tick(self) -> bool:
        """Run one probe, fire the edge callbacks, and return the probe result."""
        try:
            ok = bool(self._probe())
        except Exception as exc:  # noqa: BLE001 — a raising probe is a failure
            logger.debug("watchdog probe raised: %s", exc)
            ok = False

        step = next_watch_state(
            self._state, ok, self._consecutive_failures, self._failures_to_alert
        )
        self._state = step.state
        self._consecutive_failures = step.failures
        if step.recover:
            self._on_recover()
        if step.wedge:
            self._on_wedge(step.failures)
        # Read the state after ``on_wedge`` ran: a handler that called
        # ``rearm()`` has declared itself un-alerted, so no failing tick fires.
        if step.failing and self._state is WatchState.WEDGED and self._on_failing is not None:
            self._on_failing(step.failures)
        return ok

    def run(
        self, stop: threading.Event, interval_s: float = DEFAULT_INTERVAL_S
    ) -> None:
        """Poll until ``stop`` is set.

        The first probe fires after one full interval, which gives the webapp its
        startup window — probing immediately would race the spawn this watchdog
        exists to supervise.
        """
        while not stop.wait(interval_s):
            self.tick()


def process_identity(pid: int) -> int | None:
    """A token naming the live process ``pid``, or ``None`` when none is running.

    On Windows the token is the process creation time, so a pid the OS has
    reused for an unrelated process reads as a different identity and is never
    stopped in the webapp's place. On POSIX it is ``0`` for any live pid (no
    reuse protection there).
    """
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            return 0  # exists, owned by someone else
        return 0
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    query_limited_information = 0x1000
    still_active = 259
    handle = k32.OpenProcess(query_limited_information, False, pid)
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != still_active:
            return None
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(kernel), ctypes.byref(user),
        ):
            return None
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        k32.CloseHandle(handle)


def stop_process_tree(pid: int) -> None:
    """Stop ``pid`` and, on Windows, every process it spawned (``taskkill /T /F``).

    A webapp launched through a ``python.exe`` shim or a server that forks
    workers leaves the real listener as a descendant, so stopping only the
    tracked pid would orphan the copy that holds the port.
    """
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
            timeout=_STOP_TIMEOUT_S,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


class RespawnOutcome(Enum):
    """What :meth:`RespawnGuard.respawn` did."""

    #: A new child was spawned (any previous one was stopped or had exited).
    SPAWNED = "spawned"
    #: Refused: the child spawned last is alive and inside its startup window.
    STARTING = "starting"


class RespawnGuard:
    """Owns the one webapp child a tray spawned, so a respawn never stacks a copy.

    Every spawn the tray makes — the boot spawn, a watchdog respawn, a menu
    restart — goes through :meth:`respawn` or is reported with :meth:`track`.
    The guard then keeps three promises:

    * while the child it spawned last is alive and younger than
      ``startup_window_s``, :meth:`respawn` refuses and returns
      :attr:`RespawnOutcome.STARTING` (the handler backs off and ``rearm()``\\ s);
    * past the window, that child and its process tree are stopped before the
      next spawn, and a child that survives the stop blocks the spawn (raises);
    * the breadcrumb names the pid it stopped, or the pid that had already
      exited, before every spawn.

    Each child stopped for never getting healthy doubles the next child's
    window (up to 8x), so a boot slower than the base window is backed off
    rather than cut short forever; :meth:`mark_healthy`, called from
    ``on_recover``, resets it.

    ``spawn`` must return the new child's pid *without* waiting for readiness:
    a spawn that raises on a readiness timeout loses the pid of a child that is
    still alive, which is exactly how copies used to stack. Readiness is the
    watchdog probe's job. ``identity``, ``stop_tree`` and ``clock`` are
    injectable for tests only.
    """

    def __init__(
        self,
        log: Callable[[str], None],
        startup_window_s: float = DEFAULT_STARTUP_WINDOW_S,
        *,
        identity: Callable[[int], int | None] = process_identity,
        stop_tree: Callable[[int], None] = stop_process_tree,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._log = log
        self._base_window = startup_window_s
        self._window = startup_window_s
        self._identity = identity
        self._stop_tree = stop_tree
        self._clock = clock
        # Boot spawn and watchdog ticks run on different threads.
        self._lock = threading.Lock()
        self._pid: int | None = None
        self._pid_identity: int | None = None
        self._spawned_at = 0.0

    @property
    def pid(self) -> int | None:
        """The pid of the child spawned last, alive or not; ``None`` before any."""
        return self._pid

    def track(self, pid: int) -> None:
        """Adopt ``pid`` as the current child (a spawn made outside :meth:`respawn`)."""
        with self._lock:
            self._adopt(pid)

    def mark_healthy(self) -> None:
        """The current child answered: reset the startup window to its base."""
        with self._lock:
            self._window = self._base_window

    def is_starting(self) -> bool:
        """True while the current child is alive and inside its startup window.

        A failing probe in this state means the webapp is still booting (bound
        and not answering yet, or not bound yet): back off, never spawn or alert.
        """
        with self._lock:
            return self._alive() and self._age() < self._window

    def respawn(self, spawn: Callable[[], int]) -> RespawnOutcome:
        """Spawn a replacement child unless the current one is still starting.

        Raises whatever ``spawn`` raises, and ``RuntimeError`` when the previous
        child survives its stop, so the caller can ``rearm()`` and try again.
        """
        with self._lock:
            if self._pid is not None:
                if self._alive():
                    age = self._age()
                    if age < self._window:
                        self._log(
                            f"respawn refused: webapp pid {self._pid} still starting "
                            f"({age:.0f}s of {self._window:.0f}s startup window)"
                        )
                        return RespawnOutcome.STARTING
                    self._log(
                        f"stopping webapp pid {self._pid} and its tree before respawn "
                        f"(not healthy after {age:.0f}s)"
                    )
                    self._stop_tree(self._pid)
                    if self._alive():
                        raise RuntimeError(
                            f"webapp pid {self._pid} survived its stop; "
                            "refusing to spawn a second copy"
                        )
                    self._window = min(
                        self._window * 2, self._base_window * _MAX_WINDOW_BACKOFF
                    )
                else:
                    self._log(f"previous webapp pid {self._pid} already exited")
            new_pid = spawn()
            self._adopt(new_pid)
            return RespawnOutcome.SPAWNED

    def _adopt(self, pid: int) -> None:
        self._pid = pid
        self._pid_identity = self._identity(pid)
        self._spawned_at = self._clock()
        self._log(f"tracking webapp pid {pid}")

    def _alive(self) -> bool:
        if self._pid is None or self._pid_identity is None:
            return False
        return self._identity(self._pid) == self._pid_identity

    def _age(self) -> float:
        return self._clock() - self._spawned_at


class BreadcrumbLog:
    """Append-only, best-effort, timestamped breadcrumb file bound to one path.

    A ``pythonw``-hosted tray has no console, so ``logging``'s default stderr
    handler discards everything — including the traceback that explains why the
    webapp never came up. Give the tray one of these (conventionally
    ``<project>/webapp/watchdog.log``, gitignored by the ``*.log`` rule) and
    write a line at every start attempt, retry, wedge, respawn and recovery.

    Every write is best-effort: a full disk or a read-only directory must never
    take the tray down, so :meth:`write` swallows ``OSError`` after mirroring the
    message to the normal logger at debug level.

    ``max_bytes`` caps unbounded growth on a tray that runs for months: when the
    file exceeds it, the current file is rotated to ``<name>.1`` (replacing any
    previous one) and a fresh file started. Pass ``max_bytes=0`` to disable.
    """

    def __init__(self, path: Path, max_bytes: int = 1_000_000) -> None:
        self.path = path
        self.max_bytes = max_bytes

    def write(self, msg: str) -> None:
        """Append ``msg`` with an ISO-second timestamp. Never raises."""
        logger.debug("watchdog: %s", msg)
        try:
            # runtime-write-exempt: the breadcrumb log path is handed in by the tray, under its runtime folder
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate_if_needed()
            stamp = datetime.datetime.now().isoformat(timespec="seconds")
            # runtime-write-exempt: the breadcrumb log path is handed in by the tray, under its runtime folder
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(f"{stamp} {msg}\n")
        except OSError as exc:
            logger.debug("breadcrumb write to %s failed: %s", self.path, exc)

    def _rotate_if_needed(self) -> None:
        if self.max_bytes <= 0 or not self.path.exists():
            return
        if self.path.stat().st_size < self.max_bytes:
            return
        self.path.replace(self.path.with_name(self.path.name + ".1"))

    def __call__(self, msg: str) -> None:
        """Alias for :meth:`write` so a bound instance reads like a log call."""
        self.write(msg)


__all__ = [
    "DEFAULT_FAILURES_TO_ALERT",
    "DEFAULT_INTERVAL_S",
    "DEFAULT_STARTUP_RETRY_DELAYS_S",
    "DEFAULT_STARTUP_WINDOW_S",
    "BreadcrumbLog",
    "HealthWatchdog",
    "RespawnGuard",
    "RespawnOutcome",
    "WatchState",
    "WatchStep",
    "next_watch_state",
    "process_identity",
    "retry_with_backoff",
    "stop_process_tree",
]
