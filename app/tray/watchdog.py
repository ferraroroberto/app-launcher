"""Webapp health watchdog (issue #386).

The tray is the only long-lived process watching the webapp, and a wedged
uvicorn still LISTENs — so every existing check (port probe, adopt-or-spawn)
passes while the phone spins. The watchdog polls ``/healthz`` and turns "it
stopped answering" into a loud, timestamped breadcrumb + toast at the moment
it happens, instead of a mystery discovered hours later.

Recovery stays manual (``tray.bat --restart``) until the failure mode is
understood — this deliberately does NOT restart anything.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

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
    cadence.
    """

    def __init__(
        self,
        probe: Callable[[], bool],
        on_wedge: Callable[[int], None],
        on_recover: Callable[[], None],
        failures_to_alert: int = DEFAULT_FAILURES_TO_ALERT,
    ) -> None:
        self._probe = probe
        self._on_wedge = on_wedge
        self._on_recover = on_recover
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
        return False

    def run(
        self, stop: threading.Event, interval_s: float = DEFAULT_INTERVAL_S
    ) -> None:
        """Poll until ``stop`` is set. First probe fires after one interval,
        giving the webapp its startup window."""
        while not stop.wait(interval_s):
            self.tick()
