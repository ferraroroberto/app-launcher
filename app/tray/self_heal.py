"""Bounded self-heal for the services the tray watches.

The tray's :class:`~app.tray.watchdog.HealthWatchdog` says *that* a service
stopped answering; this module decides *whether* to restart it now and keeps
a restart from turning into a crash loop. Two rules:

* **Wait out a wedge that may clear itself.** A webapp that still holds its
  port but does not answer ``/healthz`` has historically recovered on its own
  within minutes (event-loop stalls under host load), so it gets a grace
  period. A webapp with nothing listening will not come back by itself and is
  restarted at the alert.
* **Never faster than the budget.** Each attempt doubles the wait before the
  next one, and an hourly cap stops a webapp that exits immediately from
  being respawned forever.

Every decision leaves a ``SELF-HEAL`` line in ``webapp/watchdog.log`` through
the injected ``log`` callable, so the next wedge is diagnosable from disk.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Callable, Deque, Optional

from app.webapp.manager import HEALTH_ANSWERING, HEALTH_DOWN

logger = logging.getLogger(__name__)

# Extra failed probes (60 s apart) a bound-but-silent webapp gets after the
# alert before it is restarted. The self-clearing wedges on record cleared
# within 7 minutes of the alert; 10 leaves headroom over that.
WEDGE_GRACE_PROBES = 10
DEFAULT_MAX_PER_HOUR = 3
DEFAULT_BASE_BACKOFF_S = 120.0
DEFAULT_MAX_BACKOFF_S = 1800.0
_HOUR_S = 3600.0


def should_heal_webapp(health: str, failures: int, failures_to_alert: int) -> bool:
    """Whether a failing webapp is due a restart on this probe.

    ``down`` (nothing listening) is due at once; ``bound_not_answering`` only
    after :data:`WEDGE_GRACE_PROBES` more failures, since it may clear on its
    own; ``answering`` never.
    """
    if health == HEALTH_ANSWERING:
        return False
    if health == HEALTH_DOWN:
        return True
    return failures >= failures_to_alert + WEDGE_GRACE_PROBES


class RestartBudget:
    """Exponential back-off between attempts plus a sliding hourly cap.

    ``reset()`` (called when the service answers again) clears the back-off
    streak but keeps the hourly history, so a service that flaps up and
    down still cannot be restarted more than ``max_per_hour`` times an hour.
    """

    def __init__(
        self,
        max_per_hour: int = DEFAULT_MAX_PER_HOUR,
        base_backoff_s: float = DEFAULT_BASE_BACKOFF_S,
        max_backoff_s: float = DEFAULT_MAX_BACKOFF_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_per_hour = max_per_hour
        self._base_backoff_s = base_backoff_s
        self._max_backoff_s = max_backoff_s
        self._clock = clock
        self._attempts: Deque[float] = deque()
        self._streak = 0

    def blocked_reason(self) -> Optional[str]:
        """``None`` when an attempt may run now, else why it may not."""
        now = self._clock()
        while self._attempts and now - self._attempts[0] >= _HOUR_S:
            self._attempts.popleft()
        if len(self._attempts) >= self._max_per_hour:
            wait = _HOUR_S - (now - self._attempts[0])
            return (
                f"hourly cap reached ({self._max_per_hour} attempts in the last "
                f"hour), next allowed in {wait:.0f}s"
            )
        if self._streak and self._attempts:
            backoff = min(
                self._base_backoff_s * 2 ** (self._streak - 1), self._max_backoff_s
            )
            remaining = backoff - (now - self._attempts[-1])
            if remaining > 0:
                return f"backing off {backoff:.0f}s after attempt {self._streak}, {remaining:.0f}s left"
        return None

    def record(self) -> None:
        self._attempts.append(self._clock())
        self._streak += 1

    def reset(self) -> None:
        self._streak = 0


class SelfHealer:
    """Run one service's restart action inside its :class:`RestartBudget`.

    ``action`` returns a short outcome string for the log, or raises; either
    way the attempt counts against the budget, so a restart that fails
    outright is retried no faster than one that "succeeds" into a crash.
    """

    def __init__(
        self,
        name: str,
        action: Callable[[], str],
        budget: RestartBudget,
        log: Callable[[str], None],
    ) -> None:
        self._name = name
        self._action = action
        self._budget = budget
        self._log = log
        self._deferral_logged = False

    def attempt(self, trigger: str) -> bool:
        """Restart if the budget allows. Returns True when the action ran."""
        reason = self._budget.blocked_reason()
        if reason is not None:
            # One line per deferral streak, not one per 60 s probe.
            if not self._deferral_logged:
                self._deferral_logged = True
                self._log(f"SELF-HEAL {self._name} deferred ({trigger}): {reason}")
            return False
        self._deferral_logged = False
        self._budget.record()
        self._log(f"SELF-HEAL {self._name} restarting ({trigger})")
        try:
            outcome = self._action()
        except Exception as exc:  # noqa: BLE001 — a failed restart is logged, never raised into the watchdog thread
            self._log(f"SELF-HEAL {self._name} restart failed: {type(exc).__name__}: {exc}")
            return True
        self._log(f"SELF-HEAL {self._name} {outcome}")
        return True

    def recovered(self) -> None:
        self._budget.reset()
        self._deferral_logged = False
