"""Publish a "worker cap raised" event into the chief mailbox.

A Settings save that frees lane slots (a higher ``chief_worker_cap``, or the
cap scope going ``all`` -> ``chief``) writes one ``kind: "cap-raised"`` v2
event, so the standing chief refills the new slots at once instead of waiting
for some lane to finish. The doorbell (:mod:`app.webapp.chief_doorbell`) rings
for it like any pending event; nothing here rings it directly. The file format
in fleet-config ``docs/chief-mailbox.md`` is the whole interface; nothing here
imports from fleet-config.

A lowered or unchanged save frees nothing and writes nothing. No chief-live
check: an event written while no chief runs is dropped by the readers past
their TTL. Non-canonical instances (a disposable gate webapp, a worktree)
never write the real mailbox. Best-effort: a failed write is logged and
swallowed, so it can never fail the settings save.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from src import instance_role
from src.chief_session_end import EVENT_VERSION, publish as publish_event

logger = logging.getLogger(__name__)

KIND = "cap-raised"
EVENT_NAME = "CapRaised"

#: The scope that counts every live session; switching away from it frees the
#: slots the user's own sessions were using.
_SCOPE_ALL = "all"
_SCOPE_CHIEF = "chief"


def frees_slots(
    old_cap: int, new_cap: int, old_scope: str, new_scope: str
) -> bool:
    """Whether the change leaves the chief more room than before."""
    return new_cap > old_cap or (
        old_scope == _SCOPE_ALL and new_scope == _SCOPE_CHIEF
    )


def build_event(
    old_cap: int,
    new_cap: int,
    *,
    event_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """The contract's field set; ``old``/``new`` are equal for a scope switch."""
    return {
        "v": EVENT_VERSION,
        "id": event_id or uuid.uuid4().hex,
        "kind": KIND,
        "event": EVENT_NAME,
        "old": old_cap,
        "new": new_cap,
        "launcher_sid": None,
        "repo": None,
        "number": None,
        "agent": None,
        "dispatch_id": None,
        "ts": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def publish_cap_raised(old_cfg: Any, new_cfg: Any) -> Optional[Path]:
    """Publish one cap-raised event if the save freed slots, else ``None``."""
    try:
        old_cap = int(old_cfg.chief_worker_cap)
        new_cap = int(new_cfg.chief_worker_cap)
        if not frees_slots(
            old_cap,
            new_cap,
            str(old_cfg.chief_worker_cap_scope),
            str(new_cfg.chief_worker_cap_scope),
        ):
            return None
        canonical, reason = instance_role.canonical_instance()
        if not canonical:
            logger.info(f"ℹ️ cap-raised event skipped: non-canonical instance ({reason})")
            return None
        path = publish_event(
            Path(new_cfg.chief_inbox_dir), build_event(old_cap, new_cap)
        )
    except Exception as exc:  # noqa: BLE001 — never fail the settings save
        logger.warning(f"⚠️ cap-raised event not published: {exc}")
        return None
    logger.info(f"✅ cap-raised event {old_cap} → {new_cap} → {path.name}")
    return path
