"""The chief's fallback wake.

The doorbell (:mod:`app.webapp.chief_doorbell`) rings the chief within
seconds of new mail, but it holds off whenever a fact is unknown, and it can
be switched off. Nothing else wakes the chief until the owner types. This
check is the backstop: every :data:`TICK_SECONDS` it asks whether the chief
has been left waiting for ``WebappConfig.chief_fallback_minutes`` (N), and if
so it rings once with the doorbell's own line. It rings when either holds:

* a **mailbox event has waited** longer than N minutes, judged by the publish
  time in its file name (a stale claim by its claim time), per fleet-config
  ``docs/chief-mailbox.md``;
* **worker slots are free while plan rows are queued**: the free count is the
  worker cap minus the live worker sessions, counted as the chief's own rails
  count them (alive, not external, not the chief, not a Telegram channel
  session, and under the ``chief`` cap scope only the sessions in the
  chief-managed registry).

It keeps the doorbell's safety: never while the chief is mid-turn, at a
picker or with a half-typed message in its composer. One exception: a
composer that reads as *unknown* for N minutes no longer holds the ring,
since an unknown composer would otherwise hold it forever. It never rings
more than once per N minutes, and N = 0 turns it off.

It runs whether or not the doorbell switch is on, because it is the only
wake left when the doorbell is off. Canonical instance only, like the
doorbell. Rings and skip reasons go to ``webapp/chief-doorbell.log`` through
a child of the doorbell's logger.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Set, Tuple

from app.webapp import chief_doorbell
from app.webapp.chief_doorbell import Observation
from app.webapp.route_pools import BOARD_FILES, run_on
from src._json_io import read_json_or
from src.chief.plan import read_plan_index
from src.chief.queue_status import QueueStatus, queue_status

logger = logging.getLogger(f"{chief_doorbell.__name__}.fallback")

#: How often the check looks. The smallest N is one minute, so a 30 s beat
#: rings at most half a minute late and costs two directory listings and one
#: plan read while nothing waits.
TICK_SECONDS = 30.0

_EVENT_NAME = re.compile(r"^(\d+)-(.+)\.json$")
_CHANNEL_LABEL_PREFIX = "telegram:"
_SCOPE_CHIEF = "chief"


def fallback_minutes(cfg: Any) -> int:
    """N from the config; anything but a non-negative int reads as off."""
    value = getattr(cfg, "chief_fallback_minutes", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _stamped_ages(names: Iterable[str], now_ms: int) -> List[float]:
    ages = []
    for name in names:
        match = _EVENT_NAME.match(name)
        if match is not None:
            ages.append(max(0.0, (now_ms - int(match.group(1))) / 1000.0))
    return ages


def _event_names(directory: Path) -> List[str]:
    with os.scandir(directory) as entries:
        return [e.name for e in entries if chief_doorbell.is_counted_name(e)]


def oldest_pending_age(
    inbox_dir: Path, now_ms: int, stale_ms: int = chief_doorbell.CLAIM_STALE_MS
) -> Optional[float]:
    """Seconds the oldest pending event has waited, or ``None`` when none is
    pending. Pending is the doorbell's rule: root events plus stale claims.
    A file name without a time stamp carries no age and is skipped. Raises
    ``OSError`` when the mailbox exists but cannot be listed."""
    try:
        root = _event_names(inbox_dir)
    except FileNotFoundError:
        return None
    try:
        claimed = _event_names(inbox_dir / ".claimed")
    except FileNotFoundError:
        claimed = []
    stale_s = stale_ms / 1000.0
    ages = _stamped_ages(root, now_ms)
    ages += [age for age in _stamped_ages(claimed, now_ms) if age >= stale_s]
    return max(ages) if ages else None


def queued_rows(plan_file: Path) -> Optional[int]:
    """Plan queue rows whose status is ``queued``; ``None`` when the plan
    cannot be read. No plan file is a known zero."""
    index = read_plan_index(plan_file)
    if index["state"] != "ok":
        return None
    return sum(1 for row in index["queue"] if queue_status(row) is QueueStatus.QUEUED)


def managed_sids(managed_file: Path) -> Optional[Set[str]]:
    """Session ids the chief recorded at dispatch; ``None`` when the registry
    is present but unreadable. A missing registry is an empty set."""
    if not managed_file.exists():
        return set()
    rows = read_json_or(managed_file, None, "chief-managed registry", expect=dict)
    return None if rows is None else set(rows)


def busy_workers(
    cards: List[Dict[str, Any]], chief_sid: Optional[str], managed: Optional[Set[str]]
) -> int:
    """Live worker sessions, counted as fleet-config's ``alive_worker_count``
    counts them. ``managed`` of ``None`` counts every session (``all`` scope)."""
    return sum(
        1
        for c in cards
        if c.get("alive") is True
        and c.get("kind") != "external"
        and not str(c.get("label") or "").startswith(_CHANNEL_LABEL_PREFIX)
        and c.get("label") != "chief"
        and str(c.get("session_id") or "") != (chief_sid or "")
        and (managed is None or str(c.get("session_id") or "") in managed)
    )


@dataclass(frozen=True)
class Facts:
    """One check's facts. ``None`` in a count means it could not be read."""

    #: Seconds the oldest pending event has waited; ``None`` when none is pending.
    oldest_age: Optional[float]
    queued: Optional[int]
    free_slots: Optional[int] = None
    #: The chief, read only when a reason to ring exists.
    chief: Optional[Observation] = None


def free_slots(cfg: Any, cards: List[Dict[str, Any]], chief_sid: Optional[str]) -> Optional[int]:
    """The worker cap minus the busy workers; ``None`` when the chief-managed
    registry the ``chief`` scope counts by cannot be read."""
    managed: Optional[Set[str]] = None
    if str(getattr(cfg, "chief_worker_cap_scope", _SCOPE_CHIEF)) == _SCOPE_CHIEF:
        managed = managed_sids(Path(cfg.chief_managed_file))
        if managed is None:
            return None
    return max(0, int(cfg.chief_worker_cap) - busy_workers(cards, chief_sid, managed))


async def observe(cfg: Any) -> Facts:
    """Read the mailbox and the plan, and the chief only when one of them
    gives a reason to ring, so the steady state reads no sessions."""
    oldest, queued = await asyncio.gather(
        run_on(BOARD_FILES, oldest_pending_age, Path(cfg.chief_inbox_dir),
               int(time.time() * 1000), chief_doorbell.claim_stale_ms(cfg)),
        run_on(BOARD_FILES, queued_rows, Path(cfg.chief_plan_file)),
    )
    period = fallback_minutes(cfg) * 60
    if not ((oldest is not None and oldest >= period) or queued):
        return Facts(oldest_age=oldest, queued=queued)
    obs, cards = await chief_doorbell.read_chief(cfg, None)
    slots = None
    if obs.blocked is None:
        slots = await run_on(BOARD_FILES, free_slots, cfg, cards, obs.chief_sid)
    return Facts(oldest_age=oldest, queued=queued, free_slots=slots, chief=obs)


def _reason(facts: Facts, period: float) -> Optional[str]:
    if facts.oldest_age is not None and facts.oldest_age >= period:
        return f"mailbox event waited {facts.oldest_age / 60:.0f} min"
    if facts.free_slots and facts.queued:
        return f"{facts.free_slots} free slot(s) while {facts.queued} plan row(s) queued"
    return None


class Fallback:
    """The once-per-N-minutes state machine. Pure: time comes in as an
    argument, so tests drive it with a fake clock."""

    def __init__(self) -> None:
        self._last_ring: Optional[float] = None
        self._unknown_since: Optional[float] = None

    def decide(self, facts: Facts, minutes: int, now: float) -> Tuple[str, Optional[str]]:
        """``("ring", reason)``, or a skip reason and ``None``."""
        if minutes <= 0:
            self._last_ring = None
            self._unknown_since = None
            return "off", None
        period = minutes * 60.0
        if self._last_ring is not None and now - self._last_ring < period:
            return "rang within the last period", None
        reason = _reason(facts, period)
        obs = facts.chief
        if reason is None or obs is None:
            self._unknown_since = None
            return "nothing waiting", None
        if obs.blocked:
            return obs.blocked, None
        if obs.idle is not True:
            return ("chief status unknown" if obs.idle is None
                    else f"chief not idle ({obs.status})"), None
        if obs.dirty is True:
            self._unknown_since = None
            return "composer dirty", None
        if obs.dirty is None:
            if self._unknown_since is None:
                self._unknown_since = now
            if now - self._unknown_since < period:
                return "composer unknown", None
            reason += f"; composer unknown for {(now - self._unknown_since) / 60:.0f} min"
        self._unknown_since = None
        self._last_ring = now
        return "ring", reason


async def tick_once(
    cfg: Any,
    check: Fallback,
    skips: Any,
    *,
    now: Callable[[], float] = time.monotonic,
    observe_fn: Callable[[Any], Awaitable[Facts]] = observe,
    ring_fn: Callable[..., Awaitable[str]] = chief_doorbell.ring,
) -> str:
    """One check. Returns ``"ring"``, ``"ring failed"`` or the skip reason."""
    tick_now = now()
    minutes = fallback_minutes(cfg) if cfg is not None else 0
    if minutes <= 0:
        check.decide(Facts(oldest_age=None, queued=None), 0, tick_now)
        skips.note("off", None, tick_now)
        return "off"
    try:
        facts = await observe_fn(cfg)
    except Exception as exc:  # noqa: BLE001 — a failed read is a skip, never a crash
        reason = f"observe failed: {exc}"
        skips.note(reason, None, tick_now)
        return reason
    decision, why = check.decide(facts, minutes, tick_now)
    if decision != "ring":
        skips.note(decision, facts.chief, tick_now)
        return decision
    skips.reset()
    return await ring_fn(cfg, facts.chief, f"fallback ({why})", log=logger)


async def tick_forever(app: Any) -> None:
    """The lifespan loop. Reads ``app.state.webapp_config`` every check."""
    check = Fallback()
    skips = chief_doorbell.SkipLog("fallback", logger)
    while True:
        try:
            await tick_once(getattr(app.state, "webapp_config", None), check, skips)
        except Exception:  # noqa: BLE001 — never take the lifespan down
            logger.exception("❌ chief fallback tick failed")
        await asyncio.sleep(30.0)  # TICK_SECONDS, a literal for the request-budget scan
