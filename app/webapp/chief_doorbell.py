"""The chief doorbell (#1645, step 5/7 of fleet-config#1372).

Every :data:`TICK_SECONDS` the canonical webapp checks three facts about the
standing fleet chief and, when all hold, types one fixed, data-free line into
its terminal (:data:`DOORBELL_LINE`):

* the chief is **idle at its prompt**: its Board status, computed through the
  same overlay ``GET /api/board`` uses (:func:`src.board.merge_sessions`), is
  ``idle-finished`` or ``awaiting-input``. ``tool-pending`` (a permission
  prompt) and ``awaiting-decision`` (a picker) are not idle: a typed line
  would land in the dialog, not the composer. ``waiting-on-task`` (its own
  background work, whose notification wakes it) is not idle either;
* its composer is **clean**: the session-host's ``composer_dirty`` (#1643) is
  ``False``. ``None`` (unknown, or a session-host too old to send it) never
  rings;
* its mailbox holds **pending** events, counted by file name alone per
  fleet-config ``docs/chief-mailbox.md``: root ``*.json`` plus stale claims.

The ring is level-triggered. The condition must hold for :data:`DEBOUNCE_SECONDS`
first, so lanes that finish together become one ring. After a ring the bell
waits :data:`BACKOFF_SECONDS`; a chief still idle with pending events after
that never got the ring, so it rings again. Seeing the chief busy, or the
mailbox empty, means the ring landed, and the back-off ends early.

Off unless ``WebappConfig.chief_doorbell`` is on (default off; the owner turns
it on from Settings > Chief > Doorbell, fleet-config#1457). The config is re-read every
tick, so a Settings save takes effect without a restart. Canonical instance
only, like the coverage tick: a disposable gate webapp must never type into a
real session.

The line carries no count, issue number or worker text. Everything the chief
acts on comes from ``chief_ops.py inbox`` (fleet-config#622).

Breadcrumbs go to ``webapp/chief-doorbell.log``: every ring, and every skip
reason when it changes (a 3 s tick logging each skip would be ~29k lines a day).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from src import board
from src.clients import session as session_client

from app.webapp.routers import board_chief
from app.webapp.routers.board_spawn import _read_live_sessions

logger = logging.getLogger(__name__)

#: The contract's fixed line (fleet-config ``docs/chief-mailbox.md``, "Doorbell line").
DOORBELL_LINE = "⟳ chief-inbox: pending — run chief_ops.py inbox"

TICK_SECONDS = 3.0
DEBOUNCE_SECONDS = 3.0
BACKOFF_SECONDS = 60.0

#: A ``.claimed/`` claim older than this counts as pending again
#: (``chief_ops.CLAIM_STALE_SECONDS``).
CLAIM_STALE_MS = 900_000

_CLAIM_NAME = re.compile(r"^(\d+)-(.+)\.json$")

#: Board statuses that mean "at its prompt, nothing pending on screen".
IDLE_STATUSES = frozenset({"idle-finished", "awaiting-input"})


def doorbell_on(cfg: object) -> bool:
    """Only a real ``True`` arms it: it types into a live session, so a
    config without the key (a stub) or a non-boolean reads as off."""
    return getattr(cfg, "chief_doorbell", False) is True


def _is_counted_name(entry: Any) -> bool:
    return (
        entry.name.endswith(".json")
        and not entry.name.startswith(".")
        and entry.is_file()
    )


def count_pending(inbox_dir: Path, now_ms: int) -> Optional[int]:
    """Pending events in ``inbox_dir`` by the mailbox contract's counting rule.

    ``R + S``: regular, non-dot ``*.json`` files in the root, plus those in
    ``.claimed/`` whose claim is stale (or carries no readable time). Never
    opens a file. A missing directory is ``0``; one present but not listable
    is ``None`` (unknown), never ``0``.
    """
    try:
        with os.scandir(inbox_dir) as entries:
            root = sum(1 for e in entries if _is_counted_name(e))
    except FileNotFoundError:
        return 0
    except OSError:
        return None
    claimed_dir = inbox_dir / ".claimed"
    try:
        with os.scandir(claimed_dir) as entries:
            names = [e.name for e in entries if _is_counted_name(e)]
    except FileNotFoundError:
        return root
    except OSError:
        return None
    stale = 0
    for name in names:
        match = _CLAIM_NAME.match(name)
        if match is None or now_ms - int(match.group(1)) >= CLAIM_STALE_MS:
            stale += 1
    return root + stale


@dataclass(frozen=True)
class Observation:
    """One tick's facts. ``None`` in a field means it could not be read."""

    pending: Optional[int]
    chief_sid: Optional[str] = None
    status: Optional[str] = None
    dirty: Optional[bool] = None
    #: Set when a fact could not be established (host unreadable, no chief).
    blocked: Optional[str] = None

    @property
    def idle(self) -> Optional[bool]:
        return None if self.status is None else self.status in IDLE_STATUSES

    def summary(self) -> str:
        return (
            f"idle={self.idle} status={self.status} dirty={self.dirty} "
            f"pending={self.pending} chief={(self.chief_sid or '-')[:8]}"
        )


async def observe(cfg: Any) -> Observation:
    """Read the chief's facts, the file and session reads off the loop.

    The pending count runs first and an empty mailbox stops there, so the
    steady state costs two directory listings and no session reads. The label
    reconcile stays on the loop, as in ``GET /api/board``: it schedules the
    chief-pointer write with ``create_task``.
    """
    pending = await asyncio.to_thread(
        count_pending, Path(cfg.chief_inbox_dir), int(time.time() * 1000)
    )
    if not pending:
        return Observation(pending=pending)
    (live, host_error), state = await asyncio.gather(
        asyncio.to_thread(_read_live_sessions, cfg.session_host_port),
        asyncio.to_thread(board.read_sessions_state, Path(cfg.sessions_state_file)),
    )
    if host_error is not None:
        return Observation(pending=pending, blocked=f"session-host unreadable: {host_error}")
    live = board_chief._reconcile_chief_labels(live, state["rows"])
    chief = board_chief._find_chief(live)
    if not chief:
        return Observation(pending=pending, blocked="no live chief")
    sid = str(chief.get("session_id") or "")
    # A session-host that predates the composer field sends none: unknown.
    dirty = chief.get("composer_dirty")
    if not isinstance(dirty, bool):
        dirty = None
    status = None
    cards = await asyncio.to_thread(board.merge_sessions, live, state["rows"])
    for card in cards:
        if str(card.get("session_id") or "") == sid:
            status = str(card.get("status") or "") or None
            break
    return Observation(pending=pending, chief_sid=sid, status=status, dirty=dirty)


class Doorbell:
    """The debounce + back-off state machine. Pure: time comes in as an
    argument, so tests drive it without sleeping."""

    def __init__(self) -> None:
        self._eligible_since: Optional[float] = None
        self._last_ring: Optional[float] = None

    def decide(self, obs: Observation, now: float) -> str:
        """``"ring"``, or the reason not to."""
        if obs.blocked:
            self._eligible_since = None
            return obs.blocked
        if obs.pending is None:
            self._eligible_since = None
            return "pending unknown"
        if obs.pending <= 0:
            # The chief drained its mailbox: the last ring (if any) landed.
            self._eligible_since = None
            self._last_ring = None
            return "mailbox empty"
        if obs.idle is not True:
            self._eligible_since = None
            if obs.idle is False:
                # Busy: the chief took the ring, or is working anyway. A
                # later idle with pending events is a new condition.
                self._last_ring = None
                return f"chief not idle ({obs.status})"
            return "chief status unknown"
        if obs.dirty is not False:
            self._eligible_since = None
            return "composer dirty" if obs.dirty else "composer unknown"
        if self._last_ring is not None and now - self._last_ring < BACKOFF_SECONDS:
            return "backing off after ring"
        if self._eligible_since is None:
            self._eligible_since = now
        if now - self._eligible_since < DEBOUNCE_SECONDS:
            return "debouncing"
        self._eligible_since = None
        self._last_ring = now
        return "ring"


class _SkipLog:
    """Log a skip reason only when it changes."""

    def __init__(self) -> None:
        self._last: Optional[str] = None

    def note(self, reason: str, obs: Optional[Observation]) -> None:
        if reason == self._last:
            return
        self._last = reason
        detail = f" ({obs.summary()})" if obs is not None else ""
        logger.info("🔕 chief doorbell skip: %s%s", reason, detail)

    def reset(self) -> None:
        self._last = None


async def tick_once(
    cfg: Any,
    bell: Doorbell,
    skips: _SkipLog,
    *,
    now: Callable[[], float] = time.monotonic,
    observe_fn: Callable[[Any], Awaitable[Observation]] = observe,
    send_fn: Callable[..., Dict[str, Any]] = session_client.send_input,
) -> str:
    """One tick. Returns the decision (``"ring"``, a skip reason, or ``"off"``)."""
    if cfg is None or not doorbell_on(cfg):
        skips.note("off", None)
        return "off"
    try:
        obs = await observe_fn(cfg)
    except Exception as exc:  # noqa: BLE001 — a failed read is a skip, never a crash
        reason = f"observe failed: {exc}"
        skips.note(reason, None)
        return reason
    decision = bell.decide(obs, now())
    if decision != "ring":
        skips.note(decision, obs)
        return decision
    skips.reset()
    try:
        result = await asyncio.to_thread(
            send_fn, cfg.session_host_port, obs.chief_sid, DOORBELL_LINE, True
        )
    except session_client.SessionHostError as exc:
        # The back-off already started: a host that refuses the write is
        # retried in BACKOFF_SECONDS, not hammered every tick.
        logger.warning("⚠️ chief doorbell ring failed: %s (%s)", exc, obs.summary())
        return "ring failed"
    logger.info(
        "🔔 chief doorbell rang (%s) reason=%s",
        obs.summary(),
        (result or {}).get("reason"),
    )
    return "ring"


async def tick_forever(app: Any) -> None:
    """The lifespan loop. Reads ``app.state.webapp_config`` every tick."""
    bell = Doorbell()
    skips = _SkipLog()
    while True:
        try:
            await tick_once(getattr(app.state, "webapp_config", None), bell, skips)
        except Exception:  # noqa: BLE001 — never take the lifespan down
            logger.exception("❌ chief doorbell tick failed")
        await asyncio.sleep(TICK_SECONDS)
