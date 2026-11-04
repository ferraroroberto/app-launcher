"""Daily GitHub totals across the fleet, read once and cached on disk.

Per repo and local calendar day: lines added and removed (GitHub's own
``additions`` / ``deletions`` as they are), PRs merged, issues opened and
issues closed. The repos are the fleet registry's (``src.fleet_scope``); one
with no GitHub remote is skipped and listed in ``skipped``, never counted as
zero.

Caching contract:

* :func:`snapshot` is a pure read of ``webapp/progress-totals.json`` and never
  calls GitHub, so the last result is served at once, however old.
* :func:`refresh` runs only when asked, never on a poll. A repo with no cursor
  is read in full (the backfill); afterwards each refresh re-reads a window
  that starts :data:`OVERLAP_DAYS` before the stored cursor and replaces those
  days, so a commit pushed late (its date is the author's, not the push's) is
  still counted and a repeat read never double counts.
* A failed read keeps the repo's previous days and reports ``stale``; a repo
  that was never read reports ``unknown``, never zero. The store is written
  after every repo, so an interrupted backfill resumes where it stopped.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src import fleet_scope
from src._json_io import atomic_write_json
from src.clients import github as github_client
from src.git_utils import run_git

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Module-level so tests redirect it at a tmp_path.
TOTALS_FILE = PROJECT_ROOT / "webapp" / "progress-totals.json"

METRICS = ("lines_added", "lines_removed", "prs_merged", "issues_opened", "issues_closed")

# A commit keeps the date it was made, so one pushed days later lands behind
# the cursor; a week back catches the usual lag.
OVERLAP_DAYS = 7
# GraphQL allows 5,000 points an hour; a refresh stops well before the budget
# is spent so the Board's own reads keep working.
MIN_REMAINING_POINTS = 200

OK = "ok"
STALE = "stale"
UNKNOWN = "unknown"

_STORE_VERSION = 1
_GITHUB_REMOTE = re.compile(
    r"^(?:https?://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([^/\s]+)/([^/\s]+?)(?:\.git)?/?$"
)

# One refresh at a time; the flag is what a reader sees as ``in_flight``.
_run_lock = threading.Lock()
_in_flight = False

Reader = Callable[[str, str, Optional[datetime]], Dict[str, Any]]


class TotalsError(RuntimeError):
    """The repo list could not be read, so there is nothing to refresh."""


def _empty_store() -> Dict[str, Any]:
    return {"version": _STORE_VERSION, "refreshed_at": None, "error": None, "repos": {}, "skipped": []}


def _load(path: Path) -> Tuple[Dict[str, Any], Optional[str]]:
    """``(store, problem)``; an unreadable file is the problem text, with an empty store."""
    if not path.is_file():
        return _empty_store(), None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("⚠️ progress totals: cannot read %s: %s", path, exc)
        return _empty_store(), f"The saved totals could not be read: {exc}"
    if not isinstance(data, dict) or not isinstance(data.get("repos"), dict):
        logger.warning("⚠️ progress totals: %s has an unknown shape", path)
        return _empty_store(), "The saved totals have an unknown shape."
    store = _empty_store()
    store.update({k: data[k] for k in ("refreshed_at", "error", "repos", "skipped") if k in data})
    return store, None


def _state(entry: Dict[str, Any]) -> str:
    if not entry.get("cursor"):
        return UNKNOWN
    return STALE if entry.get("error") else OK


def snapshot(path: Optional[Path] = None) -> Dict[str, Any]:
    """The saved totals with each repo's state; never calls GitHub."""
    store, problem = _load(path or TOTALS_FILE)
    repos = {
        name: {
            "state": _state(entry),
            "owner": entry.get("owner"),
            "read_at": entry.get("read_at"),
            "error": entry.get("error"),
            "days": entry.get("days") or {},
        }
        for name, entry in sorted(store["repos"].items())
    }
    return {
        "refreshed_at": store["refreshed_at"],
        "in_flight": _in_flight,
        "error": problem or store["error"],
        "repos": repos,
        "skipped": list(store["skipped"]),
    }


def parse_remote(url: str) -> Optional[Tuple[str, str]]:
    """``(owner, repo)`` of a GitHub remote URL, or None for any other host."""
    found = _GITHUB_REMOTE.match((url or "").strip())
    return (found.group(1), found.group(2)) if found else None


def resolve_repos(
    fleet_config_dir: str, projects_dir: str
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """``(readable, skipped)``: the registry's repos with a GitHub ``origin``, and why the rest are not.

    Blocking (one ``git`` per repo); :class:`TotalsError` when the registry cannot be read.
    """
    listing = fleet_scope.list_repos(fleet_config_dir)
    if not listing.get("available"):
        raise TotalsError(str(listing.get("reason") or "The fleet registry could not be read."))
    readable: List[Dict[str, str]] = []
    skipped: List[Dict[str, str]] = []
    for row in listing["repos"]:
        name = str(row["name"])
        checkout = Path(projects_dir) / name
        if not checkout.is_dir():
            skipped.append({"repo": name, "reason": "no checkout"})
            continue
        remote = parse_remote(run_git(checkout, ["remote", "get-url", "origin"]) or "")
        if remote is None:
            skipped.append({"repo": name, "reason": "no GitHub remote"})
            continue
        readable.append({"name": name, "owner": remote[0], "repo": remote[1]})
    return readable, skipped


def _local_day(stamp: str, tz: Optional[tzinfo]) -> str:
    moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return moment.astimezone(tz).date().isoformat()


def _window(cursor: Optional[str], tz: Optional[tzinfo]) -> Tuple[Optional[datetime], Optional[str]]:
    """``(since, first day)`` of the re-read window; ``(None, None)`` is the full backfill."""
    if not cursor:
        return None, None
    last = datetime.fromisoformat(cursor.replace("Z", "+00:00")).astimezone(tz)
    first = last.date() - timedelta(days=OVERLAP_DAYS)
    return github_client.local_day_window(first, tz)[0], first.isoformat()


def _aggregate(
    raw: Dict[str, Any], first_day: Optional[str], tz: Optional[tzinfo]
) -> Dict[str, Dict[str, int]]:
    """Group a repo's raw events into per-day totals, dropping days before ``first_day``."""
    days: Dict[str, Dict[str, int]] = defaultdict(lambda: dict.fromkeys(METRICS, 0))
    for stamp, added, removed in raw.get("commits") or []:
        row = days[_local_day(stamp, tz)]
        row["lines_added"] += added
        row["lines_removed"] += removed
    for key, metric in (("merged", "prs_merged"), ("opened", "issues_opened"), ("closed", "issues_closed")):
        for stamp in raw.get(key) or []:
            days[_local_day(stamp, tz)][metric] += 1
    return {d: dict(row) for d, row in sorted(days.items()) if not first_day or d >= first_day}


def _read_repo(
    entry: Dict[str, Any], reader: Reader, started: str, tz: Optional[tzinfo]
) -> Optional[int]:
    """Read one repo and fold it into ``entry``; returns GitHub's points left.

    Raises on any failure *before* touching ``entry``, so a failed read keeps
    the previous days.
    """
    since, first_day = _window(entry.get("cursor"), tz)
    raw = reader(entry["owner"], entry["repo"], since)
    fresh = _aggregate(raw, first_day, tz)
    kept = {d: row for d, row in (entry.get("days") or {}).items() if first_day and d < first_day}
    entry.update(days={**kept, **fresh}, cursor=started, read_at=started, error=None)
    return raw.get("remaining") if isinstance(raw.get("remaining"), int) else None


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def refresh(
    fleet_config_dir: str,
    projects_dir: str,
    *,
    path: Optional[Path] = None,
    now: Optional[datetime] = None,
    tz: Optional[tzinfo] = None,
    reader: Optional[Reader] = None,
) -> Dict[str, Any]:
    """Read every fleet repo now and replace the saved totals; returns :func:`snapshot`.

    Blocking and GitHub-heavy: call it on the owner's demand only, off the
    event loop. A refresh already running makes this a plain :func:`snapshot`.
    """
    global _in_flight
    target = path or TOTALS_FILE
    if not _run_lock.acquire(blocking=False):
        return snapshot(target)
    _in_flight = True
    try:
        _refresh_locked(fleet_config_dir, projects_dir, target, now, tz, reader or github_client.repo_history)
    finally:
        _in_flight = False
        _run_lock.release()
    return snapshot(target)


def _refresh_locked(
    fleet_config_dir: str,
    projects_dir: str,
    path: Path,
    now: Optional[datetime],
    tz: Optional[tzinfo],
    reader: Reader,
) -> None:
    started = _stamp(now or datetime.now(timezone.utc))
    store, _ = _load(path)
    try:
        repos, skipped = resolve_repos(fleet_config_dir, projects_dir)
    except TotalsError as exc:
        logger.warning("⚠️ progress totals: %s", exc)
        store["error"] = str(exc)
        _save(path, store)
        return
    store["error"] = None
    store["skipped"] = skipped
    failed = 0
    low_points = False
    for repo in repos:
        entry = store["repos"].setdefault(repo["name"], {"days": {}})
        entry.update(owner=repo["owner"], repo=repo["repo"])
        if low_points:
            entry["error"] = "GitHub rate limit is low; read again later."
            failed += 1
            _save(path, store)
            continue
        try:
            remaining = _read_repo(entry, reader, started, tz)
        except Exception as exc:  # one repo's failure must not lose the others
            level = logger.warning if isinstance(exc, github_client.GhError) else logger.exception
            level("⚠️ progress totals: %s failed: %s", repo["name"], exc)
            entry["error"] = f"{type(exc).__name__}: {exc}"
            failed += 1
        else:
            low_points = remaining is not None and remaining < MIN_REMAINING_POINTS
        _save(path, store)
    if failed < len(repos) or not repos:
        store["refreshed_at"] = started
    _save(path, store)
    logger.info(
        "✅ progress totals: %d read, %d failed, %d skipped (no remote)",
        len(repos) - failed, failed, len(skipped),
    )


def _save(path: Path, store: Dict[str, Any]) -> None:
    atomic_write_json(path, store)


def start_refresh(fleet_config_dir: str, projects_dir: str, **kwargs: Any) -> bool:
    """Refresh in a background thread unless one is running; True when it started."""
    if _in_flight:
        return False
    threading.Thread(
        target=refresh, args=(fleet_config_dir, projects_dir), kwargs=kwargs,
        name="progress-totals-refresh", daemon=True,
    ).start()
    return True
