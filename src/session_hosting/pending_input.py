"""Messages a session has accepted through ``/input`` but the agent has not
taken yet.

A message sent to a busy agent waits in one of two places, and the Chat view
lists both so the owner can see a message is already pending instead of
sending it again:

- ``in_composer``: the payload sits in the agent's input box while the
  deferred-submit watcher waits for the moment to press Enter.
- ``queued``: Enter went in mid-turn and the agent took the message into its
  own type-ahead queue, to answer when the turn yields.

:class:`PendingInputs` is the per-session list and its state machine only.
It knows nothing about the PTY: ``PtySession`` (through
:class:`~src.session_hosting.deferred_submit.DeferredSubmit`) tells it what
each write and each watcher verdict did, and supplies the screen facts
(idle at the prompt, queued markers painted) when the list is read.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Optional

PENDING_IN_COMPOSER = "in_composer"
PENDING_QUEUED = "queued"
# An in-composer payload the owner discarded: the watcher clears it with
# Ctrl-U instead of pressing Enter at the moment it would have submitted.
PENDING_DISCARDING = "discarding"

# Enough to read the message back in the Chat modal; past it the text is cut
# and flagged, since the full payload already lives in the session's log.
_TEXT_MAX_CHARS = 4000
# A bound on a list nobody drains: past it the oldest entry is forgotten.
_MAX_ENTRIES = 20
# Ctrl-U deletes to the start of the cursor's line, and on an empty line it
# joins the line above, so a message of N lines takes at most 2N presses.
# The slack covers a queued line typed straight into the terminal, which the
# host never sees as an entry but Up pulls into the composer with the rest.
# Extra presses on an empty composer do nothing (probe, 2026-11-01).
_CLEAR_SLACK_PRESSES = 6


@dataclass(frozen=True)
class PendingEntry:
    """One pending message. ``confirm_mark``/``confirm_by`` are set while a
    ``queued`` entry waits for the agent's queued markers to confirm it: the
    output position they must be painted after, and when to give up."""

    id: str
    text: str
    truncated: bool
    lines: int
    at: float
    state: str
    clean_before: bool = False
    confirm_mark: Optional[int] = None
    confirm_by: Optional[float] = None

    @property
    def confirmed(self) -> bool:
        return self.confirm_mark is None


class PendingInputs:
    """The pending messages of one session, safe to call from the request
    thread, the watcher thread and ``to_api`` at once."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: List[PendingEntry] = []

    def add(
        self,
        text: str,
        at: float,
        state: str,
        *,
        clean_before: bool = False,
        confirm_mark: Optional[int] = None,
        confirm_by: Optional[float] = None,
    ) -> PendingEntry:
        entry = PendingEntry(
            id=uuid.uuid4().hex[:12],
            text=text[:_TEXT_MAX_CHARS],
            truncated=len(text) > _TEXT_MAX_CHARS,
            lines=text.count("\n") + 1,
            at=at,
            state=state,
            clean_before=clean_before,
            confirm_mark=confirm_mark,
            confirm_by=confirm_by,
        )
        with self._lock:
            self._entries.append(entry)
            del self._entries[:-_MAX_ENTRIES]
        return entry

    def entries(self) -> List[PendingEntry]:
        with self._lock:
            return list(self._entries)

    def get(self, entry_id: str) -> Optional[PendingEntry]:
        with self._lock:
            return next((e for e in self._entries if e.id == entry_id), None)

    def has_state(self, *states: str) -> bool:
        with self._lock:
            return any(e.state in states for e in self._entries)

    def composer_held(self) -> List[PendingEntry]:
        """The entries whose text is in the composer right now."""
        with self._lock:
            return [
                e for e in self._entries
                if e.state in (PENDING_IN_COMPOSER, PENDING_DISCARDING)
            ]

    def clear_presses(self, states: tuple) -> int:
        """Ctrl-U presses that empty a composer holding every entry in
        ``states`` (see ``_CLEAR_SLACK_PRESSES``)."""
        with self._lock:
            lines = sum(e.lines for e in self._entries if e.state in states)
        return 2 * lines + _CLEAR_SLACK_PRESSES

    def mark_discarding(self) -> List[str]:
        """Every in-composer entry, now to be cleared rather than submitted.
        The composer holds them as one block, so they go together."""
        with self._lock:
            ids = []
            for i, e in enumerate(self._entries):
                if e.state == PENDING_IN_COMPOSER:
                    self._entries[i] = replace(e, state=PENDING_DISCARDING)
                    ids.append(e.id)
            return ids

    def submitted(self, queued_at: Optional[tuple]) -> None:
        """A CR went in and took whatever the composer held with it.

        ``queued_at`` is ``(mark, confirm_by)`` when the agent's turn was
        running, so it queues the composer and the entries wait for its
        queued markers; ``None`` when the CR submitted them outright.
        """
        with self._lock:
            kept = []
            for e in self._entries:
                if e.state not in (PENDING_IN_COMPOSER, PENDING_DISCARDING):
                    kept.append(e)
                elif queued_at is not None:
                    mark, confirm_by = queued_at
                    kept.append(replace(
                        e, state=PENDING_QUEUED, confirm_mark=mark, confirm_by=confirm_by
                    ))
            self._entries = kept

    def drop_composer(self) -> None:
        """The composer's entries will not be submitted by the host any more:
        their watcher ended without an Enter (it gave up, or cleared a
        discard), or a paste-only write superseded it."""
        with self._lock:
            self._entries = [
                e for e in self._entries
                if e.state not in (PENDING_IN_COMPOSER, PENDING_DISCARDING)
            ]

    def drop_queued(self) -> List[str]:
        with self._lock:
            removed = [e.id for e in self._entries if e.state == PENDING_QUEUED]
            self._entries = [e for e in self._entries if e.state != PENDING_QUEUED]
            return removed

    def clear(self) -> None:
        with self._lock:
            self._entries = []

    def prune(self, now: float, idle: bool, markers_since: Callable[[int], bool]) -> None:
        """Settle the ``queued`` entries against what the screen says now.

        An agent idle at its prompt has no queue: it submits what it queued
        the moment its turn yields. A ``queued`` entry still waiting for
        confirmation is confirmed once ``markers_since(confirm_mark)``, and
        dropped when ``confirm_by`` passes without that, since then nothing
        showed it was queued rather than taken at once.
        """
        with self._lock:
            kept = []
            for e in self._entries:
                if e.state != PENDING_QUEUED:
                    kept.append(e)
                elif idle:
                    continue
                elif e.confirmed:
                    kept.append(e)
                elif markers_since(e.confirm_mark):
                    kept.append(replace(e, confirm_mark=None, confirm_by=None))
                elif e.confirm_by is not None and now < e.confirm_by:
                    kept.append(e)
            self._entries = kept

    def to_api(self, removable: Callable[[PendingEntry], bool]) -> List[Dict[str, Any]]:
        return [
            {
                "id": e.id,
                "text": e.text,
                "truncated": e.truncated,
                "at": e.at,
                "state": e.state,
                # A queued entry the agent has not yet shown queued.
                "confirmed": e.confirmed,
                "removable": removable(e),
            }
            for e in self.entries()
        ]
