"""Tray webapp self-heal primitives: retry-with-backoff, health watchdog, breadcrumbs.

CANONICAL, VENDORED VERBATIM from project-scaffolding. Do **not** edit this file
per-app — it is byte-identical across every fleet tray so a fix made once in the
scaffold re-propagates everywhere. Everything app-specific (the probe, the
respawn action, the log path, the toast) is passed in by the caller, never
hardcoded here, which is what keeps the file identical. Full reasoning + the
worked wiring pattern: scaffold ``docs/windows-tray.md`` (gotcha 5) +
the sister apps' tray watchdog wiring.

The tray is the only long-lived process watching the webapp, and it starts that
webapp exactly once. Two failure modes follow from that, both observed in the
field, and this module supplies one primitive for each:

* **The initial spawn loses a transient race and never retries.** The port is
  not free yet, a cert renewal is in flight, a dependency hub is not up — one
  failed ``manager.start()`` at tray boot leaves the webapp dead for the tray's
  entire lifetime. :func:`retry_with_backoff` turns that single attempt into a
  bounded, escalating retry (a spawn that failed once used to mean days of silent downtime).

* **The webapp dies or wedges later and nothing notices.** :class:`HealthWatchdog`
  is a consecutive-failure monitor with edge-triggered callbacks — it fires once
  per *transition*, not once per failing tick, so a wedge is a single alert
  rather than a toast every minute.

* **Neither event is diagnosable afterwards.** A tray launched by ``pythonw``
  has no ``sys.stderr``, so ``logging.basicConfig()`` writes into the void and
  a boot-time traceback is captured nowhere. :class:`BreadcrumbLog` appends
  timestamped lines to a real file so the *next* occurrence is diagnosable from
  disk with no live repro. This is the piece neither the stdlib default logger
  nor a ``stdout=DEVNULL`` subprocess redirection provides.

**Dead vs wedged is the caller's decision, deliberately.** This module does not
auto-kill anything: ``on_wedge`` receives the failure count and the *app* decides,
because "not listening at all" (safe to respawn) and "listening but not answering
``/healthz``" (auto-killing could mask the real fault) warrant different actions.
:meth:`HealthWatchdog.rearm` exists for the respawn branch: a handler
that already acted asks to be re-evaluated next tick instead of waiting forever
for a recovery a genuinely-dead process will never produce on its own.
"""

from __future__ import annotations

import datetime
import logging
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
    "BreadcrumbLog",
    "HealthWatchdog",
    "WatchState",
    "WatchStep",
    "next_watch_state",
    "retry_with_backoff",
]
