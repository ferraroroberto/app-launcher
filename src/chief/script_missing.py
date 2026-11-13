"""Publish a Job whose script no longer exists into the chief mailbox.

One ``kind: "job-failed"`` v2 event with ``outcome: "script-missing"`` per job
per calendar day, so the chief repoints the job before its next run instead of
after it fails. The envelope is :func:`src.chief.job_failed.build_event`'s,
minus a run: the event carries a synthetic ``run_id`` (``script-missing-<day>``)
and ``trigger: "check"``, and a reader renders an unknown outcome as-is.

The preconditions are the job-failed ones (:func:`src.chief.job_failed.chief_live`
and friends): the switch is on, this is the canonical instance, a chief is live.
The day is recorded only when the event was published, so a chief that starts
later in the day still hears about a job that is still broken.

Best-effort: every failure is logged and swallowed.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from src import instance_role, runtime_files
from src._json_io import atomic_write_json
from src.chief import job_failed
from src.chief_session_end import EVENT_VERSION
from src.chief_session_end import publish as publish_event
from src.jobs import history as jobs_history
from src.jobs.script_missing import missing_scripts

logger = logging.getLogger(__name__)

OUTCOME = "script-missing"
SEEN_FILENAME = "script-missing-seen.json"
_EXCERPT_UNAVAILABLE = "the job did not run: its script path does not exist"


def seen_path() -> Path:
    """Resolved at call time so a test redirecting the runs dir moves this too."""
    return jobs_history.JOBS_RUNS_DIR / SEEN_FILENAME


def _read_seen(path: Path) -> Dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("⚠️ script-missing seen days unreadable: %s", exc.__class__.__name__)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}


def build_event(cfg: Any, job: Any, *, day: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "v": EVENT_VERSION,
        "id": uuid.uuid4().hex,
        "kind": job_failed.KIND,
        "event": job_failed.EVENT_NAME,
        "launcher_sid": None,
        "repo": job_failed.owning_repo(job.script_path, getattr(cfg, "projects_dir", "")),
        "number": None,
        "agent": None,
        "ts": stamp,
        "dispatch_id": None,
        "job_id": job.id,
        "job_name": job.name,
        "run_id": f"script-missing-{day}",
        "exit_code": None,
        "outcome": OUTCOME,
        "trigger": "check",
        "script_path": job.script_path,
        "excerpt": None,
        "excerpt_source": None,
        "excerpt_unavailable": _EXCERPT_UNAVAILABLE,
    }


def _may_publish(cfg: Any) -> bool:
    if getattr(cfg, "chief_job_failures", False) is not True:
        return False
    canonical, reason = instance_role.canonical_instance()
    if not canonical:
        logger.info(f"ℹ️ script-missing event skipped: non-canonical instance ({reason})")
        return False
    return job_failed.chief_live(cfg, OUTCOME) is True


def observe(
    cfg: Any,
    jobs: Iterable[Any],
    *,
    now: Optional[datetime] = None,
    path: Optional[Path] = None,
) -> List[Path]:
    """Publish one event per job whose script is missing and not yet told today."""
    try:
        return _observe(cfg, list(jobs), now=now, path=path)
    except Exception as exc:  # noqa: BLE001 — a background sweep must not die
        logger.warning(f"⚠️ script-missing sweep failed: {exc}")
        return []


def _observe(cfg: Any, jobs: List[Any], *, now: Optional[datetime], path: Optional[Path]) -> List[Path]:
    target = Path(path or seen_path())
    day = (now or datetime.now()).strftime("%Y-%m-%d")
    seen = _read_seen(target)
    # A job fixed or removed forgets its day, so the next break is told afresh.
    due = missing_scripts(jobs)
    kept = {jid: d for jid, d in seen.items() if jid in due}
    fresh = [job for jid, job in due.items() if kept.get(jid) != day]
    published: List[Path] = []
    if fresh and _may_publish(cfg):
        for job in fresh:
            try:
                published.append(publish_event(Path(cfg.chief_inbox_dir), build_event(cfg, job, day=day)))
            except Exception as exc:  # noqa: BLE001 — best effort, logged
                logger.warning(f"⚠️ script-missing event for {job.id} not published: {exc}")
                continue
            kept[job.id] = day
            logger.info(f"✅ script-missing event for {job.id} → {published[-1].name}")
    if kept != seen:
        try:
            runtime_files.make_dirs(target.parent)
            atomic_write_json(target, kept)
        except (OSError, ValueError) as exc:
            logger.warning(f"⚠️ script-missing seen days not saved: {exc}")
    return published
