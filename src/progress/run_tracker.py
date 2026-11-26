"""The progress of a long run a person is watching.

Sync all and the History refresh both take long enough on a phone that a bare
hourglass reads as stuck. A run records how many items are done, the one most
recently started and a result per item, so the page can count along and then
report. Both screens read the same shape (``started_at``, ``finished_at``,
``total``, ``done``, ``current``, ``error``, ``results``).

It lives in memory only: it describes the run in flight (or the last one), not
anything worth saving. Workers write from their own threads, a reader gets a copy.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunProgress:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: Optional[Dict[str, Any]] = None

    def begin(
        self,
        total: int,
        *,
        started_at: Optional[str] = None,
        results: Iterable[Dict[str, Any]] = (),
        error: Optional[str] = None,
    ) -> None:
        """Start a run over ``total`` items, replacing the last one.

        ``results`` are items settled before the run began (a skip): they are
        listed from the first moment and not counted in ``done``.
        """
        with self._lock:
            self._state = {
                "started_at": started_at or _now(),
                "finished_at": None,
                "total": total,
                "done": 0,
                "current": None,
                "error": error,
                "results": [dict(r) for r in results],
            }

    def started(self, name: str) -> None:
        """A worker picked ``name`` up: it is the item shown while it runs."""
        with self._lock:
            if self._state is not None:
                self._state["current"] = name

    def record(self, result: Dict[str, Any]) -> None:
        """An item finished, with its outcome."""
        with self._lock:
            if self._state is not None:
                self._state["results"].append(dict(result))
                self._state["done"] += 1

    def finish(self, error: Optional[str] = None, *, finished_at: Optional[str] = None) -> None:
        """End the run; without ``error``, one set at :meth:`begin` stays."""
        with self._lock:
            if self._state is not None:
                self._state["finished_at"] = finished_at or _now()
                self._state["current"] = None
                if error is not None:
                    self._state["error"] = error

    def snapshot(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if self._state is None:
                return None
            return {**self._state, "results": [dict(r) for r in self._state["results"]]}
