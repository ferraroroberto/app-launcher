"""Fleet-wide GitHub reads for the Board tab, via the ``gh`` CLI (issue #300).

Wraps the exact cross-repo queries the ``/issue-triage`` skill runs — open
issues, open PRs, and today's merged/closed items across every repo the
configured owner has — behind a module-level cache. The contract mirrors the
Coding tab's ⎇ git-status button: **``gh`` is a subprocess per call and is
never invoked on a poll** — :func:`refresh` runs the searches only when the
user asks (or once on first Board activation), while :func:`snapshot` is a
pure memory read the 5s board poll can hit for free.

Shared ownership note (#164 ↔ #251): this module is the single fetch layer —
the future Issues tab (#251) builds its rich inbox on these same functions
instead of growing a second ``gh`` wrapper.

Errors degrade, never break the board: a failed refresh keeps the previous
snapshot's data and surfaces ``error`` so the UI can badge it.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Sequence

logger = logging.getLogger(__name__)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_GH_TIMEOUT_SECONDS = 20.0
_ISSUE_LIMIT = 100
_PR_LIMIT = 50

# Fields common to issue and PR search rows.
_SEARCH_FIELDS = "repository,number,title,url,updatedAt"


class GhError(RuntimeError):
    """``gh`` missing, timed out, unauthenticated, or non-zero exit."""


def _run_gh(args: Sequence[str], *, timeout: float = _GH_TIMEOUT_SECONDS) -> str:
    """Run ``gh <args>`` and return stdout; :class:`GhError` on any failure."""
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GhError(f"gh {' '.join(args[:2])}: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        first = detail[0] if detail else "no output"
        raise GhError(f"gh {' '.join(args[:2])} exited {proc.returncode}: {first}")
    return proc.stdout


def _search_json(args: Sequence[str]) -> List[Dict[str, Any]]:
    out = _run_gh(args)
    try:
        data = json.loads(out or "[]")
    except ValueError as exc:
        raise GhError(f"gh returned unparseable JSON: {exc}") from exc
    return data if isinstance(data, list) else []


def _repo_name(row: Dict[str, Any]) -> str:
    repo = row.get("repository") or {}
    full = str(repo.get("nameWithOwner") or repo.get("name") or "")
    return full.split("/")[-1]


def _norm_issue(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "kind": "issue",
        "repo": _repo_name(row),
        "number": row.get("number"),
        "title": row.get("title"),
        "url": row.get("url"),
        "updated_at": row.get("updatedAt"),
        "labels": [
            lab.get("name")
            for lab in (row.get("labels") or [])
            if isinstance(lab, dict) and lab.get("name")
        ],
    }


def _norm_pr(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "kind": "pr",
        "repo": _repo_name(row),
        "number": row.get("number"),
        "title": row.get("title"),
        "url": row.get("url"),
        "updated_at": row.get("updatedAt"),
        "is_draft": bool(row.get("isDraft")),
    }


def search_open_issues(owner: str) -> List[Dict[str, Any]]:
    rows = _search_json([
        "search", "issues",
        "--owner", owner,
        "--state", "open",
        "--sort", "updated",
        "--limit", str(_ISSUE_LIMIT),
        "--json", _SEARCH_FIELDS + ",labels",
    ])
    return [_norm_issue(r) for r in rows]


def search_open_prs(owner: str) -> List[Dict[str, Any]]:
    rows = _search_json([
        "search", "prs",
        "--owner", owner,
        "--state", "open",
        "--sort", "updated",
        "--limit", str(_PR_LIMIT),
        "--json", _SEARCH_FIELDS + ",isDraft",
    ])
    return [_norm_pr(r) for r in rows]


def search_done_today(owner: str) -> List[Dict[str, Any]]:
    """Merged PRs + closed issues since local midnight, newest first."""
    since = date.today().isoformat()
    merged = _search_json([
        "search", "prs",
        "--owner", owner,
        "--merged",
        "--merged-at", f">={since}",
        "--sort", "updated",
        "--limit", str(_PR_LIMIT),
        "--json", _SEARCH_FIELDS,
    ])
    closed = _search_json([
        "search", "issues",
        "--owner", owner,
        "--state", "closed",
        "--closed", f">={since}",
        "--sort", "updated",
        "--limit", str(_ISSUE_LIMIT),
        "--json", _SEARCH_FIELDS,
    ])
    done: List[Dict[str, Any]] = []
    for row in merged:
        done.append({**_norm_pr(row), "state": "merged"})
    for row in closed:
        done.append({**_norm_issue(row), "state": "closed", "labels": []})
    done.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return done


# ------------------------------------------------------------------ cache

_lock = threading.Lock()
_cache: Dict[str, Any] = {
    "fetched_at": None,
    "issues": [],
    "prs": [],
    "done": [],
    "error": None,
}


def snapshot() -> Dict[str, Any]:
    """The cached GitHub view — a pure memory read, safe on every poll."""
    with _lock:
        return dict(_cache)


def refresh(owner: str) -> Dict[str, Any]:
    """Run the ``gh`` searches now and replace the cache.

    Subprocess-heavy (four ``gh`` calls) — callers invoke this on explicit
    user demand only, never on a poll. On failure the previous data is kept
    and only ``error`` is updated, so a flaky ``gh`` degrades to a badge
    instead of an empty board.
    """
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    try:
        issues = search_open_issues(owner)
        prs = search_open_prs(owner)
        done = search_done_today(owner)
    except GhError as exc:
        logger.warning("⚠️ gh refresh failed: %s", exc)
        with _lock:
            _cache["error"] = str(exc)
            return dict(_cache)
    with _lock:
        _cache.update(
            fetched_at=fetched_at, issues=issues, prs=prs, done=done, error=None
        )
        logger.info(
            "✅ gh refresh: %d issues, %d PRs, %d done today",
            len(issues), len(prs), len(done),
        )
        return dict(_cache)


def reset_cache() -> None:
    """Test helper — restore the pristine empty cache."""
    with _lock:
        _cache.update(fetched_at=None, issues=[], prs=[], done=[], error=None)
