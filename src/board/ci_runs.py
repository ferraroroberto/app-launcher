"""Red GitHub CI on each fleet repo's default branch, for the Board and the chief.

The Board's GitHub refresh (``POST /api/board/github/refresh``) starts
:func:`start_refresh`, which reads every fleet repo that has a workflow file
with one ``gh api .../actions/runs`` call each and replaces the in-memory
cache. ``GET /api/board`` turns the cache into cards with :func:`board_cards`,
a pure memory read. After each refresh the readable results go to
:func:`src.chief.ci_failed.observe`, which publishes the chief's mailbox event.

A repo is ``red`` when the latest finished run of any of its workflows failed,
``green`` when none did, and ``unknown`` when its runs could not be read or
its default branch could not be named: an unreadable repo is never green.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src import fleet_scope
from src.board.state import age_seconds, parse_iso
from src.clients import github as github_client
from src.git_utils import run_git

logger = logging.getLogger(__name__)

STATE_RED = "red"
STATE_GREEN = "green"
STATE_UNKNOWN = "unknown"

#: The conclusions the mailbox contract calls a failure.
RED_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})
#: GitHub's run status once a run has finished.
_GH_COMPLETED = "completed"
#: The Board card's state for a red workflow, as a failed Job card says it.
CARD_FAILED = "failed"
#: A run that ends neither way (cancelled, skipped, neutral) says nothing
#: about the branch, so the streak looks past it.
_GREEN_CONCLUSIONS = frozenset({"success"})

#: The Board's own staleness re-refresh comes every two minutes; one fleet
#: sweep a minute is the most GitHub ever sees from here.
REFRESH_COOLDOWN_SECONDS = 60
#: A handful of ``gh`` calls in flight keeps a sweep of ~30 repos short.
_WORKERS = 4

_lock = threading.Lock()
_cache: Dict[str, Any] = {"fetched_at": None, "repos": {}, "error": None, "refreshing": False}
_last_refresh_started: Optional[float] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def ci_repos(fleet_config_dir: str, projects_dir: str) -> List[Dict[str, Any]]:
    """The fleet's non-dormant repos that hold a workflow file, with their paths.

    Raises :class:`RuntimeError` when the fleet repo list cannot be read.
    """
    scope = fleet_scope.list_repos(fleet_config_dir)
    if not scope["available"]:
        raise RuntimeError(f"fleet repo list unreadable: {scope['reason']}")
    repos = []
    for row in scope["repos"]:
        if row["status"] == fleet_scope.DORMANT:
            continue
        path = Path(projects_dir) / row["name"]
        workflows = path / ".github" / "workflows"
        if any(workflows.glob("*.yml")) or any(workflows.glob("*.yaml")):
            repos.append({"name": row["name"], "path": path})
    return repos


def default_branch(repo_path: Path) -> Optional[str]:
    """The branch ``origin/HEAD`` names in the local clone, else ``None``."""
    ref = run_git(repo_path, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if not ref or not ref.startswith("origin/"):
        return None
    return ref[len("origin/"):] or None


def summarize(runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The red workflows among ``runs`` (newest first): one entry per workflow.

    Each entry is the workflow's latest finished run, with ``red_since`` the
    creation time of the oldest failure in its unbroken red streak.
    """
    by_workflow: Dict[Any, List[Dict[str, Any]]] = {}
    for run in runs:
        if run.get("status") != _GH_COMPLETED:
            continue
        conclusion = run.get("conclusion")
        if conclusion not in RED_CONCLUSIONS and conclusion not in _GREEN_CONCLUSIONS:
            continue
        by_workflow.setdefault(run.get("workflow_id") or run.get("name"), []).append(run)
    red = []
    for decisive in by_workflow.values():
        latest = decisive[0]
        if latest.get("conclusion") not in RED_CONCLUSIONS:
            continue
        streak_start = latest
        for run in decisive[1:]:
            if run.get("conclusion") not in RED_CONCLUSIONS:
                break
            streak_start = run
        red.append({
            "workflow": str(latest.get("name") or "?"),
            "run_id": latest.get("id"),
            "run_url": latest.get("html_url"),
            "head_sha": latest.get("head_sha"),
            "event": latest.get("event"),
            "conclusion": latest.get("conclusion"),
            "created_at": latest.get("created_at"),
            "red_since": streak_start.get("created_at"),
        })
    return red


def read_repo(owner: str, repo: Dict[str, Any]) -> Dict[str, Any]:
    """One repo's CI state; every failure is an ``unknown`` with its reason."""
    branch = default_branch(repo["path"])
    if branch is None:
        return {"state": STATE_UNKNOWN, "reason": "default branch not known (origin/HEAD unset)", "red": []}
    try:
        runs = github_client.workflow_runs(owner, repo["name"], branch)
    except github_client.GhError as exc:
        return {"state": STATE_UNKNOWN, "reason": str(exc), "branch": branch, "red": []}
    red = summarize(runs)
    return {"state": STATE_RED if red else STATE_GREEN, "reason": "", "branch": branch, "red": red}


def refresh(
    owner: str,
    fleet_config_dir: str,
    projects_dir: str,
    on_result: Optional[Callable[[Dict[str, Dict[str, Any]]], None]] = None,
) -> Dict[str, Any]:
    """Read every CI repo now and replace the cache; ``on_result`` sees the repos."""
    fetched_at = _now_iso()
    try:
        repos = ci_repos(fleet_config_dir, projects_dir)
        with ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="ci-runs") as pool:
            states = list(pool.map(lambda r: read_repo(owner, r), repos))
        results = {repo["name"]: state for repo, state in zip(repos, states, strict=True)}
    except Exception as exc:  # noqa: BLE001 — a failed sweep keeps the older data
        logger.warning("⚠️ CI runs: sweep failed: %s", exc)
        with _lock:
            _cache.update(error=str(exc), refreshing=False)
        return snapshot()
    with _lock:
        _cache.update(fetched_at=fetched_at, repos=results, error=None, refreshing=False)
    counts = {s: sum(1 for r in results.values() if r["state"] == s)
              for s in (STATE_RED, STATE_GREEN, STATE_UNKNOWN)}
    logger.info(
        "✅ CI runs: %d repos, %d red, %d green, %d unknown",
        len(results), counts[STATE_RED], counts[STATE_GREEN], counts[STATE_UNKNOWN],
    )
    if on_result is not None:
        try:
            on_result(results)
        except Exception as exc:  # noqa: BLE001 — the Board's data is already in
            logger.warning("⚠️ CI runs: result hook failed: %s", exc)
    return snapshot()


def start_refresh(
    owner: str,
    fleet_config_dir: str,
    projects_dir: str,
    on_result: Optional[Callable[[Dict[str, Dict[str, Any]]], None]] = None,
    *,
    now: Optional[float] = None,
) -> bool:
    """Refresh in a background thread unless one runs or ran within the cooldown."""
    global _last_refresh_started
    tick = time.monotonic() if now is None else now
    with _lock:
        if _cache["refreshing"]:
            return False
        if _last_refresh_started is not None and tick - _last_refresh_started < REFRESH_COOLDOWN_SECONDS:
            return False
        _cache["refreshing"] = True
        _last_refresh_started = tick
    threading.Thread(
        target=refresh, args=(owner, fleet_config_dir, projects_dir, on_result),
        name="ci-runs-refresh", daemon=True,
    ).start()
    return True


def snapshot() -> Dict[str, Any]:
    """The cached sweep — a pure memory read, safe on every poll."""
    with _lock:
        return {**_cache, "repos": dict(_cache["repos"])}


def section(snap: Dict[str, Any]) -> Dict[str, Any]:
    """The Board payload's ``ci`` block: ``available: false`` until a sweep lands."""
    return {
        "available": snap.get("fetched_at") is not None,
        "fetched_at": snap.get("fetched_at"),
        "error": snap.get("error"),
        "refreshing": bool(snap.get("refreshing")),
    }


def board_cards(snap: Dict[str, Any], *, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """One ``failed`` card per red workflow, one ``unknown`` card per unreadable repo."""
    moment = now or datetime.now(timezone.utc)
    cards: List[Dict[str, Any]] = []
    for name in sorted(snap.get("repos") or {}):
        result = snap["repos"][name]
        if result["state"] == STATE_UNKNOWN:
            cards.append({
                "kind": "ci", "repo": name, "state": "unknown", "workflow": None,
                "run_url": None, "age_seconds": None, "error": result.get("reason") or "",
            })
            continue
        for red in result.get("red") or []:
            since = parse_iso(red.get("red_since"))
            cards.append({
                "kind": "ci", "repo": name, "state": CARD_FAILED,
                "workflow": red["workflow"], "run_id": red["run_id"],
                "run_url": red.get("run_url"), "conclusion": red.get("conclusion"),
                "age_seconds": age_seconds(since, moment) if since else None,
                "error": None,
            })
    return cards


def reset_ci_cache() -> None:
    """Test helper — restore the pristine empty cache."""
    global _last_refresh_started
    with _lock:
        _cache.update(fetched_at=None, repos={}, error=None, refreshing=False)
        _last_refresh_started = None
