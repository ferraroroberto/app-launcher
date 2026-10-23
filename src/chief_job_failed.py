"""Publish a failed Job run into the chief mailbox.

The third failure channel of :func:`src.notifications.notify_failure`: one
``kind: "job-failed"`` v2 event per failed run, dropped as a file into the
chief's mailbox so the doorbell (:mod:`app.webapp.chief_doorbell`) wakes the
chief. The file format in fleet-config ``docs/chief-mailbox.md`` is the whole
interface; nothing here imports from fleet-config.

An event is published only when all four hold, checked cheapest first:

* ``WebappConfig.chief_job_failures`` is on;
* this is the canonical instance (:func:`src.instance_role.canonical_instance`),
  so a disposable gate webapp or a worktree never writes the real mailbox;
* the session-host lists a live chief, so events do not pile up for a chief
  that is not running. A session list that could not be read is "not
  confirmed", and publishes nothing;
* the run's outcome is a failure or an unconfirmed delivery (the caller has
  already dropped a deferred exit code).

Best-effort like the other channels: every failure is logged and swallowed.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from src import board, instance_role, session_client
from src.chief_session_end import EVENT_VERSION, publish as publish_event
from src.jobs_history import read_output_tail, read_run

logger = logging.getLogger(__name__)

KIND = "job-failed"
EVENT_NAME = "JobFailed"
#: The contract's excerpt cap: the last 1,500 characters of the run's output.
EXCERPT_CHARS = 1500
#: Enough bytes that 1,500 characters are available even for multi-byte text.
_TAIL_BYTES = 16 * 1024

_TRIGGER_CHAIN_PREFIX = "chain:"
_TRIGGER_SCHEDULED = "scheduled"


def chief_live(cfg: Any) -> Optional[bool]:
    """Whether the session-host lists a live chief; ``None`` when unreadable.

    The identity rules are the Board's: the labelled session, or a chief that
    was started by hand and self-heals through ``board_chief``. Imported at
    call time because ``app`` sits above ``src`` and this runs only on a
    failed run, not at executor start-up.
    """
    from app.webapp.routers import board_chief

    try:
        live = session_client.list_sessions(cfg.session_host_port)
    except session_client.SessionHostError as exc:
        logger.info(f"ℹ️ job-failed event skipped: session list unreadable ({exc})")
        return None
    state = board.read_sessions_state(Path(cfg.sessions_state_file))
    return bool(board_chief._find_chief(
        board_chief._reconcile_chief_labels(live, state["rows"])
    ))


def _event_trigger(raw: Any) -> str:
    """The run record's trigger in the contract's vocabulary."""
    text = str(raw or "").strip()
    if text.startswith(_TRIGGER_CHAIN_PREFIX):
        return "chain"
    if text == _TRIGGER_SCHEDULED:
        return "schedule"
    return text or "manual"


def owning_repo(script_path: str, projects_dir: str) -> Optional[str]:
    """The fleet repo whose folder holds ``script_path``, else ``None``.

    A repo is a direct child of ``projects_dir``; a script outside it, a
    relative path or an unset ``projects_dir`` is "no repo", never a guess.
    """
    if not script_path or not projects_dir or not Path(script_path).is_absolute():
        return None
    script = Path(os.path.abspath(script_path))
    root = Path(os.path.abspath(projects_dir))
    try:
        # normcase only for the comparison: the repo name keeps its own case.
        Path(os.path.normcase(script)).relative_to(os.path.normcase(root))
    except ValueError:
        return None
    if len(script.parts) <= len(root.parts) + 1:
        return None
    return script.parts[len(root.parts)]


def _excerpt(run_dir: Path) -> Dict[str, Any]:
    tail = read_output_tail(run_dir, max_bytes=_TAIL_BYTES)
    text = tail[-EXCERPT_CHARS:].strip()
    if text:
        return {"excerpt": text, "excerpt_source": "job-output"}
    return {
        "excerpt": None,
        "excerpt_source": None,
        "excerpt_unavailable": "output.log is missing or empty",
    }


def build_event(
    cfg: Any,
    job: Any,
    run_dir: Path,
    *,
    exit_code: Optional[int],
    outcome: str,
    event_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    event: Dict[str, Any] = {
        "v": EVENT_VERSION,
        "id": event_id or uuid.uuid4().hex,
        "kind": KIND,
        "event": EVENT_NAME,
        "launcher_sid": None,
        "repo": owning_repo(job.script_path, getattr(cfg, "projects_dir", "")),
        "number": None,
        "agent": None,
        "ts": stamp,
        "dispatch_id": None,
        "job_id": job.id,
        "job_name": job.name,
        "run_id": run_dir.name,
        "exit_code": exit_code,
        "outcome": outcome,
        "trigger": _event_trigger(read_run(run_dir).get("trigger")),
        "script_path": job.script_path,
    }
    event.update(_excerpt(run_dir))
    return event


def publish_job_failed(
    cfg: Any,
    job: Any,
    run_dir: Path,
    *,
    exit_code: Optional[int],
    outcome: str,
) -> Optional[Path]:
    """Publish one job-failed event if every precondition holds, else ``None``."""
    try:
        if getattr(cfg, "chief_job_failures", False) is not True:
            return None
        canonical, reason = instance_role.canonical_instance()
        if not canonical:
            logger.info(f"ℹ️ job-failed event skipped: non-canonical instance ({reason})")
            return None
        if chief_live(cfg) is not True:
            return None
        path = publish_event(
            Path(cfg.chief_inbox_dir),
            build_event(cfg, job, run_dir, exit_code=exit_code, outcome=outcome),
        )
    except Exception as exc:  # noqa: BLE001 — never block finalisation
        logger.warning(f"⚠️ job-failed event for {job.id} not published: {exc}")
        return None
    logger.info(f"✅ job-failed event for {job.id} run {run_dir.name} → {path.name}")
    return path
