"""Per-client login lock-out: a doubling wait after repeated wrong passwords.

A web app's password route is the one endpoint a caller can hit without a
credential, so without a limit it accepts unlimited guesses. This module
bounds that. Each client gets ``free_attempts`` wrong passwords; after that,
every further failure makes it wait ``base_delay`` seconds, doubling each time
up to ``max_delay``. A client is never locked out for good, so the owner
cannot be shut out of their own app. The vendored ``sign-in`` sheet shows the
wait from the ``429`` + ``Retry-After`` reply.

Wiring, in front of the app's existing password check (which is unchanged)::

    wait = lockout.retry_after(key)
    if wait > 0:
        raise HTTPException(429, "too many attempts", headers=retry_after_header(wait))
    ...                                    # the app's own 503 and password checks
    if wrong_password:
        lockout.record_failure(key)
        raise HTTPException(401, "bad password")
    lockout.reset(key)

``key`` is the client address the app already resolves and logs. The app also
decides who skips the lock-out entirely (the PC itself, with its own trust
check): behind a local tunnel every public request can arrive as loopback, so
this module never inspects addresses or headers.

``on_alert`` is called once per failure streak when a client reaches
``alert_after`` consecutive failures, so the owner hears about a guessing
attempt early. It receives a :class:`LockoutAlert` and must return at once
(hand any network send to a thread). A global ``alert_cooldown`` limits how
often it fires; alerts it holds back are counted into the next one.

State is in memory only, so a restart clears it. The table is bounded:
forgotten clients go first, then the one whose last failure is oldest. The
log line on each lock names no client, address or password.

Vendor-verbatim (manifest key ``login_lockout``), stdlib-only. Server side of
``app/webapp/static/_vendored/sign-in/README.md``.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Five covers a fat-fingered human on a phone keyboard; the doubling past it
# turns an unbounded guess rate into a handful per hour.
FREE_ATTEMPTS = 5
BASE_DELAY_SECONDS = 2.0
MAX_DELAY_SECONDS = 300.0
# An hour with no failure ends the streak, so an old typo spree costs nothing later.
FORGET_AFTER_SECONDS = 3600.0
# A caller rotating source addresses cannot grow the table past this.
MAX_TRACKED_CLIENTS = 1024
# Three wrong passwords in a row is past a typo, early enough to act on.
ALERT_AFTER_FAILURES = 3
# At most one alert per five minutes, so an address-rotating attack is one message, not hundreds.
ALERT_COOLDOWN_SECONDS = 300.0


@dataclass(frozen=True)
class LockoutAlert:
    """What ``on_alert`` receives: never a password or a token."""

    key: str
    """The client key the app passed in (its resolved client address)."""
    failures: int
    """Consecutive failures from that client so far."""
    suppressed: int
    """Alerts the cooldown held back since the last one was sent."""


class _Streak:
    __slots__ = ("failures", "deadline", "last_failure", "alerted")

    def __init__(self) -> None:
        self.failures = 0
        self.deadline = 0.0
        self.last_failure = 0.0
        self.alerted = False


class LoginLockout:
    """Per-client failure streaks with a doubling wait and a one-shot alert."""

    def __init__(
        self,
        *,
        free_attempts: int = FREE_ATTEMPTS,
        base_delay: float = BASE_DELAY_SECONDS,
        max_delay: float = MAX_DELAY_SECONDS,
        forget_after: float = FORGET_AFTER_SECONDS,
        max_clients: int = MAX_TRACKED_CLIENTS,
        alert_after: int = ALERT_AFTER_FAILURES,
        alert_cooldown: float = ALERT_COOLDOWN_SECONDS,
        on_alert: Callable[[LockoutAlert], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if free_attempts < 0 or alert_after < 1 or max_clients < 1:
            raise ValueError("free_attempts must be >= 0, alert_after and max_clients >= 1")
        if not 0 < base_delay <= max_delay <= forget_after:
            raise ValueError("need 0 < base_delay <= max_delay <= forget_after")
        if alert_cooldown < 0:
            raise ValueError("alert_cooldown must be >= 0")
        self._free = free_attempts
        self._base = base_delay
        self._max = max_delay
        self._forget = forget_after
        self._max_clients = max_clients
        self._alert_after = alert_after
        self._cooldown = alert_cooldown
        self._on_alert = on_alert
        self._clock = clock
        self._lock = threading.Lock()
        # Ordered by last failure, oldest first, so eviction pops from the front.
        self._streaks: OrderedDict[str, _Streak] = OrderedDict()
        self._last_alert: float | None = None
        self._suppressed = 0

    def retry_after(self, key: str) -> float:
        """Seconds ``key`` must still wait; 0 means it may try now."""
        with self._lock:
            now = self._clock()
            streak = self._live(key, now)
            return max(0.0, streak.deadline - now) if streak else 0.0

    def record_failure(self, key: str) -> float:
        """Count one wrong password from ``key``; returns the wait it now faces."""
        with self._lock:
            now = self._clock()
            streak = self._live(key, now)
            if streak is None:
                self._make_room(now)
                streak = self._streaks[key] = _Streak()
            self._streaks.move_to_end(key)
            streak.failures += 1
            streak.last_failure = now
            over = streak.failures - self._free
            delay = min(self._base * 2 ** (over - 1), self._max) if over > 0 else 0.0
            streak.deadline = now + delay
            if delay:
                logger.info(f"🔒 login locked for {math.ceil(delay)} s after {streak.failures} failures")
            alert = self._take_alert(key, streak, now)
        if alert is not None and self._on_alert is not None:
            try:
                self._on_alert(alert)
            except Exception as exc:  # an alert must never break the login route
                logger.warning(f"⚠️ login alert callback failed: {type(exc).__name__}")
        return delay

    def reset(self, key: str) -> None:
        """Clear ``key``'s streak after a correct password."""
        with self._lock:
            self._streaks.pop(key, None)

    def _live(self, key: str, now: float) -> _Streak | None:
        streak = self._streaks.get(key)
        if streak is not None and now - streak.last_failure >= self._forget:
            del self._streaks[key]
            return None
        return streak

    def _make_room(self, now: float) -> None:
        while self._streaks:
            oldest = next(iter(self._streaks.values()))
            if now - oldest.last_failure < self._forget:
                break
            self._streaks.popitem(last=False)
        if len(self._streaks) >= self._max_clients:
            self._streaks.popitem(last=False)

    def _take_alert(self, key: str, streak: _Streak, now: float) -> LockoutAlert | None:
        if self._on_alert is None or streak.alerted or streak.failures < self._alert_after:
            return None
        streak.alerted = True
        if self._last_alert is not None and now - self._last_alert < self._cooldown:
            self._suppressed += 1
            logger.info(f"ℹ️ login alert held back by the cooldown ({self._suppressed} held)")
            return None
        alert = LockoutAlert(key, streak.failures, self._suppressed)
        self._last_alert = now
        self._suppressed = 0
        logger.info(f"⚠️ login alert raised after {streak.failures} consecutive failures")
        return alert


def retry_after_header(seconds: float) -> dict[str, str]:
    """The ``Retry-After`` header for a wait, rounded up to whole seconds (at least 1)."""
    return {"Retry-After": str(max(1, math.ceil(seconds)))}
