"""Publish a red GitHub CI run into the chief mailbox.

One ``kind: "ci-failed"`` v2 event per failed default-branch run, in the
format fleet-config ``docs/chief-mailbox.md`` defines; nothing here imports
from fleet-config. :func:`observe` runs after every CI sweep
(:mod:`src.board.ci_runs`) with the readable repos' results.

Dedupe is ``repo`` + ``run_id``, kept in :data:`SEEN_FILE` across restarts.
A repo seen for the first time is only recorded, never published, so turning
the feature on (or a fresh install) never backfills history. Every red run is
recorded whether or not it was published: a run that went red while the
switch was off or no chief was running stays quiet afterwards, the way a Job
failure does.

A run is published only when, checked cheapest first:

* ``WebappConfig.chief_ci_failures`` is on;
* this is the canonical instance, so a gate webapp or worktree never writes
  the real mailbox;
* the session-host lists a live chief (unreadable is "not confirmed": none).

Best-effort: every failure is logged and swallowed.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src import instance_role, runtime_files
from src._json_io import atomic_write_json
from src.board import ci_runs
from src.chief.job_failed import chief_live
from src.chief_session_end import EVENT_VERSION
from src.chief_session_end import publish as publish_event

logger = logging.getLogger(__name__)

KIND = "ci-failed"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Module-level so tests redirect it at a tmp_path (tests/conftest.py).
SEEN_FILE = PROJECT_ROOT / "webapp" / "ci-failed-seen.json"
#: Far more run ids per repo than one sweep can show as red at once.
MAX_PER_REPO = 50
_EXCERPT_UNAVAILABLE = "the launcher reads no CI logs; the run page has them"

_lock = threading.Lock()


def read_seen(path: Optional[Path] = None) -> Dict[str, List[int]]:
    """The recorded run ids per repo; ``{}`` when nothing readable is on file."""
    target = Path(path or SEEN_FILE)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("⚠️ ci-failed seen runs unreadable: %s", exc.__class__.__name__)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(repo): [i for i in ids if isinstance(i, int)][-MAX_PER_REPO:]
        for repo, ids in raw.items() if isinstance(ids, list)
    }


def build_event(repo: str, red: Dict[str, Any], *, now: Optional[datetime] = None) -> Dict[str, Any]:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "v": EVENT_VERSION,
        "id": uuid.uuid4().hex,
        "kind": KIND,
        "repo": repo,
        "workflow": red.get("workflow"),
        "run_id": red.get("run_id"),
        "run_url": red.get("run_url"),
        "head_sha": red.get("head_sha"),
        "event": red.get("event"),
        "conclusion": red.get("conclusion"),
        "created_at": red.get("created_at"),
        # Naming the failed jobs would cost a call per run; the chief reads
        # the run itself when it investigates.
        "failed_jobs": [],
        "launcher_sid": None,
        "number": None,
        "agent": None,
        "dispatch_id": None,
        "ts": stamp,
        "excerpt": None,
        "excerpt_source": None,
        "excerpt_unavailable": _EXCERPT_UNAVAILABLE,
    }


def _may_publish(cfg: Any) -> bool:
    if getattr(cfg, "chief_ci_failures", False) is not True:
        return False
    canonical, reason = instance_role.canonical_instance()
    if not canonical:
        logger.info(f"ℹ️ ci-failed event skipped: non-canonical instance ({reason})")
        return False
    return chief_live(cfg, KIND) is True


def observe(cfg: Any, results: Dict[str, Dict[str, Any]], *, path: Optional[Path] = None) -> List[Path]:
    """Record the sweep's red runs and publish the new ones; returns the paths."""
    target = Path(path or SEEN_FILE)
    published: List[Path] = []
    with _lock:
        seen = read_seen(target)
        before = json.dumps(seen, sort_keys=True)
        fresh: List[tuple] = []
        for repo, result in results.items():
            if result.get("state") == ci_runs.STATE_UNKNOWN:
                continue
            known = seen.get(repo)
            red_ids = [r["run_id"] for r in result.get("red") or [] if isinstance(r.get("run_id"), int)]
            if known is not None:
                fresh.extend((repo, r) for r in result.get("red") or [] if r.get("run_id") not in known)
            seen[repo] = ((known or []) + [i for i in red_ids if i not in (known or [])])[-MAX_PER_REPO:]
        if fresh and _may_publish(cfg):
            for repo, red in fresh:
                try:
                    published.append(publish_event(Path(cfg.chief_inbox_dir), build_event(repo, red)))
                except Exception as exc:  # noqa: BLE001 — best effort, logged
                    logger.warning(f"⚠️ ci-failed event for {repo} run {red.get('run_id')} not published: {exc}")
                    seen[repo] = [i for i in seen[repo] if i != red.get("run_id")]
                    continue
                logger.info(f"✅ ci-failed event for {repo} run {red.get('run_id')} → {published[-1].name}")
        if json.dumps(seen, sort_keys=True) == before:
            return published
        try:
            runtime_files.make_dirs(target.parent)
            atomic_write_json(target, seen)
        except (OSError, ValueError) as exc:
            logger.warning(f"⚠️ ci-failed seen runs not saved: {exc}")
    return published
