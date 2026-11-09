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
from pathlib import Path

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


class HealthWatchdog:
    """Consecutive-failure health monitor with edge-triggered callbacks.

    :meth:`tick` runs one probe. After ``failures_to_alert`` *consecutive*
    failures it fires ``on_wedge(count)`` **once** — edge-triggered, not again
    until a recovery (or an explicit :meth:`rearm`) re-arms it — and the first
    success after an alert fires ``on_recover()``.

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
        self._alerted = False

    def rearm(self) -> None:
        """Allow ``on_wedge`` to fire again on the next failing tick, without
        waiting for a recovery.

        Call this from inside ``on_wedge`` when the handler already took action
        (e.g. attempted a respawn) and wants another shot next interval if that
        action didn't fix things. Without it, a handler that respawns and fails
        stays silently alerted forever — a dead process never produces the
        recovery that would otherwise re-arm the edge.
        """
        self._alerted = False

    def tick(self) -> bool:
        """Run one probe, fire the edge callbacks, and return the probe result."""
        try:
            ok = bool(self._probe())
        except Exception as exc:  # noqa: BLE001 — a raising probe is a failure
            logger.debug("watchdog probe raised: %s", exc)
            ok = False

        if ok:
            if self._alerted:
                self._alerted = False
                self._on_recover()
            self._consecutive_failures = 0
            return True

        self._consecutive_failures += 1
        if not self._alerted and self._consecutive_failures >= self._failures_to_alert:
            self._alerted = True
            self._on_wedge(self._consecutive_failures)
        # Read ``_alerted`` after ``on_wedge`` ran: a handler that called
        # ``rearm()`` has declared itself un-alerted, so no failing tick fires.
        if self._alerted and self._on_failing is not None:
            self._on_failing(self._consecutive_failures)
        return False

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
    "retry_with_backoff",
]
