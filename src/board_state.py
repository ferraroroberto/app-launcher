"""Hook-state-file IO for the Board tab (issue #408 split of ``board.py``).

Reads the three files fleet-config's hooks/statusline/workflows write:

* the **sessions-state file** (``session_state`` hook —
  ``~/.claude/hooks/state/sessions-state.json``): ``working`` / ``needs-you``
  / ``idle`` rows per recent Claude Code session.
* the **rate-limits cache** (a statusline writer): the 5h/7d Claude usage
  percentages.
* the **active-issues file** (the shared issue workflows —
  ``~/.claude/hooks/state/active-issues.json``): issue branches currently in
  flight across the fleet.

Both share the same degradation contract (#164 acceptance): a
missing/corrupt/stale file must never error — callers get an
``available: False`` shape with empty/``None`` fields instead of an
exception, so a board render never breaks on a cold or torn state file.

Also home to the small time/parse helpers (:func:`_now`, :func:`_parse_iso`,
:func:`_iso_z`, :func:`_age_seconds`) shared by :mod:`src.board_sessions` and
:mod:`src.board_transcript` — every board-side timestamp flows through these
same two tolerant parsers.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# A state row (or the whole file) older than this is treated as gone-cold —
# matches the writer's own prune horizon in fleet-config's session_state hook.
STATE_STALE_AFTER = timedelta(hours=24)

# The rate-limits cache (issue #326) is meant to read as near-real-time — the
# statusline that writes it re-renders on every assistant message — so this
# is far shorter than STATE_STALE_AFTER's 24h (that one tolerates a cold
# session; a usage percentage sitting stale for hours would be misleading).
RATE_LIMITS_STALE_AFTER = timedelta(minutes=10)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(raw: Any) -> Optional[datetime]:
    """Tolerant ISO-8601 parse; naive stamps are assumed local (job records)."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def _iso_z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _age_seconds(anchor: Optional[datetime], now: datetime) -> Optional[int]:
    if anchor is None:
        return None
    return max(0, int((now - anchor).total_seconds()))


def read_sessions_state(path: Path, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Read the hook-written state file with full staleness tolerance.

    Absent / unreadable / corrupt → ``{"available": False, ...}`` with empty
    rows (precedent: ``life_os.recap_status`` and ``jobs._read_queue_file``).
    ``stale`` is true when the newest row is older than
    :data:`STATE_STALE_AFTER` — the hooks have stopped writing.
    """
    now = now or _now()
    empty: Dict[str, Any] = {"available": False, "stale": False, "updated_at": None, "rows": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty

    rows = {
        str(sid): row for sid, row in data.items() if isinstance(row, dict)
    }
    stamps = [
        stamp
        for stamp in (_parse_iso(row.get("updated_at")) for row in rows.values())
        if stamp is not None
    ]
    newest = max(stamps) if stamps else None
    return {
        "available": True,
        "stale": bool(newest is not None and now - newest > STATE_STALE_AFTER),
        "updated_at": _iso_z(newest) if newest else None,
        "rows": rows,
    }


def read_active_issues(path: Path, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Read fresh cross-repo issue markers with tolerant degradation.

    Fleet-config's issue workflows key rows by ``<repo>#<number>`` and stamp
    ``started_at`` once the branch is ready. Missing/corrupt input is
    unavailable; malformed and older-than-24h rows are omitted individually so
    an abandoned workflow can never disable a backlog card forever.
    """
    now = now or _now()
    empty: Dict[str, Any] = {"available": False, "updated_at": None, "rows": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty

    cutoff = now - STATE_STALE_AFTER
    rows: Dict[str, Any] = {}
    stamps = []
    for row in data.values():
        if not isinstance(row, dict):
            continue
        repo = row.get("repo")
        number = row.get("number")
        started_at = _parse_iso(row.get("started_at"))
        if (
            not isinstance(repo, str)
            or not repo.strip()
            or not isinstance(number, int)
            or isinstance(number, bool)
            or number <= 0
            or started_at is None
            or started_at < cutoff
        ):
            continue
        rows[f"{repo.strip().lower()}#{number}"] = row
        stamps.append(started_at)

    newest = max(stamps) if stamps else None
    return {
        "available": True,
        "updated_at": _iso_z(newest) if newest else None,
        "rows": rows,
    }


def _rate_limit_window(raw: Any) -> Optional[Dict[str, Any]]:
    """One ``five_hour``/``seven_day`` window, tolerant field-by-field.

    ``raw`` may be absent, ``null``, or a dict with either sub-field missing
    or ``null`` — a future writer may report a window before it has learned
    ``resets_at``, say. Any shape short of "a dict" collapses to ``None``
    (window not reported); within a dict, each sub-field is pulled
    independently so one bad field doesn't blank the other.
    """
    if not isinstance(raw, dict):
        return None
    pct = raw.get("used_percentage")
    used_percentage = pct if isinstance(pct, (int, float)) else None
    resets_at: Optional[int] = None
    raw_resets = raw.get("resets_at")
    if isinstance(raw_resets, (int, float)):
        resets_at = int(raw_resets)
    return {"used_percentage": used_percentage, "resets_at": resets_at}


def read_rate_limits(path: Path, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The cached 5h/7d Claude usage percentages a statusline writer maintains.

    Mirrors :func:`read_sessions_state`'s degradation contract exactly:
    absent/corrupt/non-dict file → ``available: False`` with both windows
    ``None``, never an error. ``stale`` is driven by the file's own
    ``captured_at`` stamp (when the writer last ran) against
    :data:`RATE_LIMITS_STALE_AFTER` — *not* by either window's ``resets_at``,
    which is the reset moment, not a freshness signal.
    """
    now = now or _now()
    empty: Dict[str, Any] = {
        "available": False,
        "stale": False,
        "updated_at": None,
        "five_hour": None,
        "seven_day": None,
    }
    try:
        # utf-8-sig: the statusline writer is an external PowerShell script
        # we don't fully control, and .NET's UTF8Encoding can default to
        # emitting a BOM — utf-8-sig strips one if present and reads plain
        # utf-8 identically otherwise, so a BOM never masquerades as a
        # corrupt file.
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty

    captured_at = _parse_iso(data.get("captured_at"))
    return {
        "available": True,
        "stale": bool(
            captured_at is not None and now - captured_at > RATE_LIMITS_STALE_AFTER
        ),
        "updated_at": _iso_z(captured_at) if captured_at else None,
        "five_hour": _rate_limit_window(data.get("five_hour")),
        "seven_day": _rate_limit_window(data.get("seven_day")),
    }
