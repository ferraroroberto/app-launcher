"""Shared atomic-JSON-write and interprocess-lock helpers.

Six call sites across ``src/`` each hand-rolled the same
tempfile-then-``os.replace`` dance with their own suffix bikeshed. This
module is the single place that owns it. It also owns the msvcrt-based
file-lock-with-retry pattern (issue #520) that both ``src.jobs_queue`` and
``src.jobs_history`` need for their own read-modify-write critical sections.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:  # Windows-only interprocess file lock (this repo runs Windows-only).
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows fallback
    msvcrt = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def atomic_write_json(target: Path, payload: Any, *, indent: int = 2) -> None:
    """Write ``payload`` as JSON to ``target`` atomically.

    Serializes to a sibling ``<name><suffix>.tmp`` file, then ``os.replace``s
    it over ``target`` — the swap is all-or-nothing, so a crash mid-write or
    a concurrent reader never observes a partially-written file. Caller is
    responsible for ensuring ``target.parent`` exists.
    """
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=indent), encoding="utf-8")
    os.replace(tmp, target)


@contextmanager
def file_lock(lock_path: Path, *, label: str) -> Iterator[None]:
    """Hold an exclusive interprocess lock for a read-modify-write section.

    Shared by :mod:`src.jobs_queue` (``_queue_file_lock``) and
    :mod:`src.jobs_history` (``_run_json_lock``) — both need to serialize a
    read-modify-``os.replace`` across genuinely separate OS processes (the
    webapp process and a spawned executor process). Uses a Windows
    ``msvcrt.locking`` byte-range lock on a dedicated ``lock_path`` sidecar
    file, never on the JSON document itself (which ``os.replace`` swaps out
    from under any held handle). Retries a few times before giving up and
    proceeding unlocked — logged via ``label`` — since losing serialization
    is far better than wedging the caller's operation forever.

    Callers resolve ``lock_path`` themselves (rather than this helper
    caching it) so tests that monkeypatch the underlying JSON path redirect
    the lock file too, instead of touching a stale production path.
    """
    if msvcrt is None:  # pragma: no cover - non-Windows fallback
        yield
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    locked = False
    try:
        for attempt in range(3):
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                locked = True
                break
            except OSError:
                if attempt == 2:
                    logger.warning(
                        "⚠️  %s lock contended — writing unlocked", label
                    )
        yield
    finally:
        if locked:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        handle.close()
