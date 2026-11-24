"""Which ``\\AppLauncher\\`` triggers are pinned to a UTC offset.

``schtasks /Query /FO LIST /V`` omits ``StartBoundary``, so this reads the
``/XML`` dump of the folder, which opens each task with
``<!-- \\AppLauncher\\<id> -->``. A trigger whose ``StartBoundary`` ends in
``Z`` or ``±HH:MM`` is an absolute instant: Task Scheduler honours it, so the
task runs an hour off the wall clock after every daylight-saving change.
Consumed by :mod:`src.jobs.coverage`.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from threading import Lock
from typing import Callable, Dict, List, Optional, Set, Tuple

from src.jobs import schtasks

logger = logging.getLogger(__name__)

_XML_TASK_COMMENT_RE = re.compile(r"<!--\s*(\\AppLauncher\\.*?)\s*-->")
_XML_START_BOUNDARY_RE = re.compile(r"<StartBoundary>\s*([^<]*?)\s*</StartBoundary>")
_UTC_OFFSET_SUFFIX_RE = re.compile(r"(?:Z|[+-]\d{2}:\d{2})$")

# ``schtasks /Query /TN \AppLauncher\`` exits 1 with this text when the folder
# was never created, which is the normal state of a machine with no scheduled
# Jobs. Matched on the English message; another locale reports unknown.
_FOLDER_MISSING_RE = re.compile(r"cannot find the (?:file|path) specified", re.IGNORECASE)

_cache: Optional[Tuple[float, Optional[Dict[str, bool]]]] = None
_lock = Lock()
# A dead query repeats every cache window; each distinct failure text is
# worth one warning per process.
_warned: Set[str] = set()


def _parse_trigger_offsets(stdout: str) -> Dict[str, bool]:
    """``{task_name: any trigger's StartBoundary carries a UTC offset}``."""
    out: Dict[str, bool] = {}
    # re.split with one group yields [preamble, name, body, name, body, ...].
    parts = _XML_TASK_COMMENT_RE.split(stdout)
    for name, body in zip(parts[1::2], parts[2::2]):
        out[name] = any(
            _UTC_OFFSET_SUFFIX_RE.search(boundary)
            for boundary in _XML_START_BOUNDARY_RE.findall(body)
        )
    return out


def registered_task_offset_pins(
    runner: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None,
) -> Optional[Dict[str, bool]]:
    """``{task_name: offset_pinned}`` for every ``\\AppLauncher\\`` task.

    ``None`` for the whole map = the query failed; callers treat that as
    *unknown*, never as "no pinned triggers". A task folder that does not
    exist holds no tasks, so it is ``{}``. Cached for the same window as
    the bulk records, so a scan costs one XML query per window.
    """
    global _cache
    now = time.monotonic()
    with _lock:
        if _cache is not None:
            ts, cached = _cache
            if now - ts < schtasks._NEXT_RUN_TTL_SECONDS:
                return cached
        # Looked up on the module at call time so tests that patch
        # ``schtasks._run_schtasks`` intercept this query too.
        proc = (runner or schtasks._run_schtasks)(
            ["schtasks", "/Query", "/TN", schtasks.TASK_FOLDER_PREFIX, "/XML"]
        )
        fresh: Optional[Dict[str, bool]]
        stderr = (proc.stderr or "").strip()
        if proc.returncode != 0 and _FOLDER_MISSING_RE.search(stderr):
            fresh = {}
        elif proc.returncode != 0 or not proc.stdout:
            detail = stderr[:200] or "<no stderr>"
            if detail not in _warned:
                _warned.add(detail)
                logger.warning(
                    "⚠️  schtasks XML query failed (rc=%s) — pinned-offset "
                    "check reports unknown: %s",
                    proc.returncode,
                    detail,
                )
            fresh = None
        else:
            fresh = _parse_trigger_offsets(proc.stdout)
        _cache = (now, fresh)
        return fresh


def invalidate_offset_pin_cache() -> None:
    """Drop the cached XML snapshot; called with the bulk-record cache."""
    global _cache
    with _lock:
        _cache = None
