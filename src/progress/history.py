"""``GET /api/progress``: the GitHub totals and the hub's token history, one answer.

Cache first and no read waits on a recalculation. The GitHub half is the saved
totals (:func:`src.progress.totals.snapshot`, no GitHub call); the token half is
the hub's cached history. ``refresh`` starts a recalculation of both in the
background and the answer is still the cache, with ``refreshing`` true while
either runs. The halves fail separately: one that cannot be read reads
``unavailable`` with a reason while the other still renders, and a metric
never computed is ``null`` / ``unknown``, never zero.

Days are the local calendar day for GitHub's numbers and the UTC day for the
hub's tokens (``day_basis``); a bucket takes each by its own label.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from src.progress import buckets, tokens, totals

logger = logging.getLogger(__name__)

OK = "ok"
STALE = "stale"
UNKNOWN = "unknown"
UNAVAILABLE = "unavailable"


def _age(stamp: Optional[str], now: datetime) -> Optional[float]:
    if not stamp:
        return None
    try:
        return round(max((now - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds(), 0.0), 1)
    except ValueError:
        return None


def _sum(rows: Any) -> Dict[str, int]:
    out = dict.fromkeys(totals.METRICS, 0)
    for row in rows:
        for metric in totals.METRICS:
            out[metric] += int(row.get(metric, 0))
    return out


def _github_state(snap: Dict[str, Any], known: Dict[str, Any]) -> tuple[str, Optional[str]]:
    if not known:
        if snap["error"]:
            return UNAVAILABLE, snap["error"]
        return UNKNOWN, "The GitHub totals have not been read yet."
    if snap["error"] or any(r["state"] == totals.STALE for r in known.values()):
        return STALE, snap["error"] or "The last read failed for some repos; showing what was kept."
    return OK, None


def _github(
    snap: Dict[str, Any], plan: List[Dict[str, Any]], first: str, last: str, now: datetime, started: bool
) -> Dict[str, Any]:
    """The GitHub half: its state, one total per bucket (``None`` when nothing is read) and per repo."""
    repos = snap["repos"]
    known = {n: r for n, r in repos.items() if r["state"] in (totals.OK, totals.STALE)}
    state, reason = _github_state(snap, known)
    per_bucket: List[Optional[Dict[str, int]]] = []
    for b in plan:
        wanted = set(b["days"])
        per_bucket.append(
            _sum(v for r in known.values() for d, v in r["days"].items() if d in wanted) if known else None
        )
    rows = [
        {
            "repo": name,
            "state": repo["state"],
            **_sum(v for d, v in repo["days"].items() if first <= d <= last),
        }
        for name, repo in sorted(known.items())
    ]
    return {
        "half": {
            "state": state,
            "reason": reason,
            "refreshed_at": snap["refreshed_at"],
            "age_seconds": _age(snap["refreshed_at"], now),
            "refreshing": bool(snap["in_flight"]) or started,
            "unknown_repos": sorted(n for n, r in repos.items() if r["state"] == totals.UNKNOWN),
            "skipped": snap["skipped"],
        },
        "buckets": per_bucket,
        "repos": rows,
    }


def _github_unreadable(exc: Exception, buckets_count: int) -> Dict[str, Any]:
    return {
        "half": {
            "state": UNAVAILABLE, "reason": f"{type(exc).__name__}: {exc}", "refreshed_at": None,
            "age_seconds": None, "refreshing": False, "unknown_repos": [], "skipped": [],
        },
        "buckets": [None] * buckets_count,
        "repos": [],
    }


def read(
    *,
    bucket: str,
    first: date,
    last: date,
    hub_url: str,
    fleet_config_dir: str,
    projects_dir: str,
    refresh: bool = False,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """The combined answer for one range. Blocking (one loopback call to the hub); never raises."""
    now = now or datetime.now(timezone.utc)
    start, stop = first.isoformat(), last.isoformat()
    plan = buckets.bucket_plan(first, last, bucket)
    started = False
    if refresh:
        try:
            started = totals.start_refresh(fleet_config_dir, projects_dir)
        except Exception:  # the token half must still be answered
            logger.exception("⚠️ progress: could not start the GitHub refresh")
    try:
        github = _github(totals.snapshot(), plan, start, stop, now, started)
    except Exception as exc:  # an unreadable store must not take the token half down
        logger.exception("⚠️ progress: GitHub totals unreadable")
        github = _github_unreadable(exc, len(plan))
    hub = tokens.read(hub_url, start, stop, refresh=refresh)
    return {
        "bucket": bucket,
        "from": start,
        "to": stop,
        "generated_at": now.isoformat(timespec="seconds"),
        "day_basis": {"github": "local", "tokens": "utc"},
        "refreshing": github["half"]["refreshing"] or hub["refreshing"],
        "github": github["half"],
        "tokens": {k: v for k, v in hub.items() if k != "days"},
        "buckets": [
            {
                "start": b["start"],
                "end": b["end"],
                "github": github["buckets"][i],
                "tokens": tokens.bucket_tokens(hub["days"], b["days"]) if hub["days"] else None,
            }
            for i, b in enumerate(plan)
        ],
        "repos": github["repos"],
    }
