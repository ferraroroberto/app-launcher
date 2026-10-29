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
from typing import Callable, Dict, List, Optional, Tuple

from src.jobs import schtasks

logger = logging.getLogger(__name__)

_XML_TASK_COMMENT_RE = re.compile(r"<!--\s*(\\AppLauncher\\.*?)\s*-->")
_XML_START_BOUNDARY_RE = re.compile(r"<StartBoundary>\s*([^<]*?)\s*</StartBoundary>")
_UTC_OFFSET_SUFFIX_RE = re.compile(r"(?:Z|[+-]\d{2}:\d{2})$")

_cache: Optional[Tuple[float, Optional[Dict[str, bool]]]] = None
_lock = Lock()


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
    *unknown*, never as "no pinned triggers". Cached for the same window as
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
        if proc.returncode != 0 or not proc.stdout:
            logger.warning(
                "⚠️  schtasks XML query failed (rc=%s) — pinned-offset check "
                "reports unknown: %s",
                proc.returncode,
                (proc.stderr or "").strip()[:200] or "<no stderr>",
            )
            fresh: Optional[Dict[str, bool]] = None
        else:
            fresh = _parse_trigger_offsets(proc.stdout)
        _cache = (now, fresh)
        return fresh


def invalidate_offset_pin_cache() -> None:
    """Drop the cached XML snapshot; called with the bulk-record cache."""
    global _cache
    with _lock:
        _cache = None
