"""The token half of ``GET /api/progress``: the hub's history, bucketed.

States of the half: ``ok``; ``stale`` (the hub's last rebuild failed, or the
hub is down and the last good answer for this range is served); ``unknown``
(the hub has never computed a history); ``unavailable`` (the hub cannot be
read and nothing is kept for this range). A day or agent without a
measurement is never zero: a bucket sums only its measured days and counts the
rest in ``unknown_days``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from src.clients import hub_token_history

logger = logging.getLogger(__name__)

# A few ranges at most: the dashboard asks for one at a time.
_KEEP_RANGES = 8

_lock = threading.Lock()
_last_good: "OrderedDict[Tuple[str, str], Tuple[float, Dict[str, Any]]]" = OrderedDict()
_failing = False


def _remember(key: Tuple[str, str], body: Dict[str, Any]) -> None:
    with _lock:
        _last_good[key] = (time.time(), body)
        _last_good.move_to_end(key)
        while len(_last_good) > _KEEP_RANGES:
            _last_good.popitem(last=False)


def _recall(key: Tuple[str, str]) -> Optional[Tuple[float, Dict[str, Any]]]:
    with _lock:
        return _last_good.get(key)


def reset() -> None:
    """Drop what is kept. Tests only."""
    global _failing
    with _lock:
        _last_good.clear()
        _failing = False


def _why(exc: hub_token_history.HubTokenHistoryError) -> str:
    if exc.status == 404:
        return "The hub has no token history route yet; restart the hub to load it."
    return str(exc)


def _note(failing: bool, reason: str = "") -> None:
    """One breadcrumb per change of state, not one per poll."""
    global _failing
    if failing and not _failing:
        logger.warning("⚠️ progress tokens: hub history unavailable: %s", reason)
    elif _failing and not failing:
        logger.info("✅ progress tokens: hub history readable again")
    _failing = failing


def read(base_url: str, first: str, last: str, *, refresh: bool = False) -> Dict[str, Any]:
    """``{"state", "reason", "computed_at", "age_seconds", "refreshing", "days"}`` for the range; never raises.

    ``days`` is the hub's day list (empty when there is nothing to show).
    """
    base = (base_url or "").strip()
    if not base:
        return _half("unavailable", "The hub is not configured (llm_hub_url is empty).")
    key = (first, last)
    try:
        body = hub_token_history.fetch(base, first, last, refresh=refresh)
    except hub_token_history.HubTokenHistoryError as exc:
        reason = _why(exc)
        _note(True, reason)
        kept = _recall(key)
        if kept is None:
            return _half("unavailable", reason)
        fetched_at, old = kept
        return _from_body(old, state="stale", reason=f"{reason} Showing the last answer.", age=time.time() - fetched_at)
    _note(False)
    _remember(key, body)
    cache = body["cache"]
    state = {"ok": "ok", "stale": "stale"}.get(cache.get("state"), "unknown")
    reason = cache.get("last_error") if state == "stale" else (
        "The hub has not computed a token history yet." if state == "unknown" else None
    )
    return _from_body(body, state=state, reason=reason, refreshing=bool(cache.get("refreshing")) or refresh)


def _half(state: str, reason: Optional[str]) -> Dict[str, Any]:
    return {"state": state, "reason": reason, "computed_at": None, "age_seconds": None, "refreshing": False, "days": []}


def _from_body(
    body: Dict[str, Any], *, state: str, reason: Optional[str], refreshing: bool = False, age: Optional[float] = None
) -> Dict[str, Any]:
    cache = body["cache"]
    return {
        "state": state,
        "reason": reason,
        "computed_at": cache.get("computed_at"),
        "age_seconds": cache.get("age_seconds") if age is None else (cache.get("age_seconds") or 0) + round(age, 1),
        "refreshing": refreshing,
        "days": body["days"],
    }


def _count(cell: Any, field: str) -> int:
    value = cell.get(field) if isinstance(cell, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def bucket_tokens(days: List[Dict[str, Any]], bucket_days: List[str]) -> Dict[str, Dict[str, Any]]:
    """Per agent: tokens in / out over the bucket's measured days.

    ``state`` is ``measured`` (at least one measured day), ``not_measured``
    (the agent has no token counts at all) or ``unknown`` (no day measured, so
    both sums are null). ``unknown_days`` counts the bucket days the hub could
    not measure for that agent.
    """
    wanted = set(bucket_days)
    rows = [row for row in days if isinstance(row, dict) and row.get("date") in wanted]
    agents = sorted({a for row in rows for a in (row.get("vendors") or {})})
    out: Dict[str, Dict[str, Any]] = {}
    for agent in agents:
        cells = [(row.get("vendors") or {}).get(agent) for row in rows]
        if all(isinstance(c, dict) and c.get("state") == "not_measured" for c in cells):
            out[agent] = {"state": "not_measured", "tokens_in": None, "tokens_out": None, "unknown_days": 0}
            continue
        measured = [c for c in cells if isinstance(c, dict) and c.get("state") == "measured"]
        if not measured:
            out[agent] = {"state": "unknown", "tokens_in": None, "tokens_out": None, "unknown_days": len(bucket_days)}
            continue
        out[agent] = {
            "state": "measured",
            "tokens_in": sum(_count(c, "tokens_in") for c in measured),
            "tokens_out": sum(_count(c, "output_tokens") for c in measured),
            "unknown_days": len(bucket_days) - len(measured),
        }
    return out
