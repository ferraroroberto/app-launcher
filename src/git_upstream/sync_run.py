"""The progress of the Sync all run a person is watching.

Sync all fast-forwards every project, which takes long enough on a phone that
a bare hourglass reads as stuck. The run records how many projects are done,
the one most recently started and a result per project, so the page can count
along and then report. The shape (``started_at``, ``finished_at``, ``total``,
``done``, ``current``, ``error``, ``results``) is the one the History
refresh records, so both screens read progress the same way.

It lives in memory only: it describes the run in flight (or the last one), not
anything worth saving. Workers write from pool threads, a reader gets a copy.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional


class SyncRun:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: Optional[Dict[str, Any]] = None

    def begin(self, total: int) -> None:
        """Start a run over ``total`` projects, replacing the last one."""
        with self._lock:
            self._state = {
                "started_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": None,
                "total": total,
                "done": 0,
                "current": None,
                "error": None,
                "results": [],
            }

    def started(self, name: str) -> None:
        """A worker picked ``name`` up: it is the project shown while it runs."""
        with self._lock:
            if self._state is not None:
                self._state["current"] = name

    def record(self, result: Dict[str, Any]) -> None:
        """A project finished, with its outcome."""
        with self._lock:
            if self._state is not None:
                self._state["results"].append(dict(result))
                self._state["done"] += 1

    def finish(self, error: Optional[str] = None) -> None:
        with self._lock:
            if self._state is not None:
                self._state["finished_at"] = datetime.now(timezone.utc).isoformat()
                self._state["current"] = None
                self._state["error"] = error

    def snapshot(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if self._state is None:
                return None
            return {**self._state, "results": [dict(r) for r in self._state["results"]]}
