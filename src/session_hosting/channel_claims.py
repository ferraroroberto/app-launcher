"""One live session per Telegram channel label.

A bot token has one ``getUpdates`` consumer, and two webapp processes can ask
for the same profile at once, so the webapp's per-process lock is not enough:
the session-host is the one place both requests meet.
"""

from __future__ import annotations

from typing import Any, Iterable, Set

from src.session_hosting.kinds import CHANNEL_LABEL_PREFIX


class DuplicateChannelSession(Exception):
    """A second session was asked for on a channel label that already has a
    live one (or one being spawned). ``session_id`` is empty for the
    in-flight case."""

    def __init__(self, label: str, session_id: str) -> None:
        self.label = label
        self.session_id = session_id
        where = (
            f"live in session {session_id[:8]}" if session_id
            else "already being started"
        )
        super().__init__(
            f"{label} is {where}; one Telegram bot can only be attached to "
            "one session."
        )


class ChannelClaims:
    """Labels whose spawn is in flight, so two concurrent creates cannot both
    pass the live check before either session registers.

    Not thread-safe by itself: the caller holds the manager's lock around
    :meth:`claim` and :meth:`release`.
    """

    def __init__(self) -> None:
        self._spawning: Set[str] = set()

    @staticmethod
    def applies(label: str) -> bool:
        return label.startswith(CHANNEL_LABEL_PREFIX)

    def claim(self, label: str, sessions: Iterable[Any]) -> None:
        """Hold ``label`` for a spawn, or raise :class:`DuplicateChannelSession`
        when a live session or an in-flight spawn already has it."""
        for session in sessions:
            if getattr(session, "label", "") == label and session.alive:
                raise DuplicateChannelSession(label, session.session_id)
        if label in self._spawning:
            raise DuplicateChannelSession(label, "")
        self._spawning.add(label)

    def release(self, label: str) -> None:
        self._spawning.discard(label)
