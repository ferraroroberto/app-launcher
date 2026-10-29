"""Webapp health watchdog (issue #386).

The tray is the only long-lived process watching the webapp, and a wedged
uvicorn still LISTENs — so every existing check (port probe, adopt-or-spawn)
passes while the phone spins. The watchdog polls ``/healthz`` and turns "it
stopped answering" into a loud, timestamped breadcrumb + toast at the moment
it happens, instead of a mystery discovered hours later.

The watchdog only detects; what to do about a failing service is the
caller's ``on_failing`` callback (the tray's bounded self-heal in
:mod:`app.tray.self_heal`), so the edge semantics stay testable on their own.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 60.0
DEFAULT_FAILURES_TO_ALERT = 3


class HealthWatchdog:
    """Consecutive-failure health monitor with edge-triggered callbacks.

    ``tick()`` runs one probe. After ``failures_to_alert`` *consecutive*
    failures it fires ``on_wedge(count)`` once — edge-triggered, not again
    until a recovery re-arms it — and the first success after an alert fires
    ``on_recover()``. The threshold absorbs a normal tray-menu webapp
    restart (a few seconds of downtime) without a false alarm at the 60 s
    cadence. ``on_failing(count)``, when given, fires on *every* failed probe
    from the alert onward, so a recovery policy can wait out a grace period
    and retry without the watchdog knowing about either.
    """

    def __init__(
        self,
        probe: Callable[[], bool],
        on_wedge: Callable[[int], None],
        on_recover: Callable[[], None],
        failures_to_alert: int = DEFAULT_FAILURES_TO_ALERT,
        on_failing: Optional[Callable[[int], None]] = None,
    ) -> None:
        self._probe = probe
        self._on_wedge = on_wedge
        self._on_recover = on_recover
        self._on_failing = on_failing
        self._failures_to_alert = failures_to_alert
        self._consecutive_failures = 0
        self._alerted = False

    def tick(self) -> bool:
        """Run one probe; fire the edge callbacks. Returns the probe result."""
        try:
            ok = bool(self._probe())
        except Exception as exc:  # noqa: BLE001 — a raising probe is a failure
            logger.debug(f"watchdog probe raised: {exc}")
            ok = False

        if ok:
            if self._alerted:
                self._alerted = False
                self._on_recover()
            self._consecutive_failures = 0
            return True

        self._consecutive_failures += 1
        if (
            not self._alerted
            and self._consecutive_failures >= self._failures_to_alert
        ):
            self._alerted = True
            self._on_wedge(self._consecutive_failures)
        if self._alerted and self._on_failing is not None:
            self._on_failing(self._consecutive_failures)
        return False

    def run(
        self, stop: threading.Event, interval_s: float = DEFAULT_INTERVAL_S
    ) -> None:
        """Poll until ``stop`` is set. First probe fires after one interval,
        giving the webapp its startup window."""
        while not stop.wait(interval_s):
            self.tick()
