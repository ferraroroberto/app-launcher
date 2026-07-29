"""Durable pointer to the fleet chief's own conversation (issue #675).

The Board's chief **Resume** used to answer purely from ``sessions-state.json``
— the hook-written rows :mod:`src.board_state` reads. #670 made that lookup
pick the *right* conversation rather than merely the newest one, but it stayed
dependent on two things that can both be absent: a row whose ``name`` is
``"chief"``, and a live PTY to prefer. Two perfectly resumable conversations
are therefore invisible to a row scan:

* a chief **the launcher never spawned** (a plain Coding-tab session in
  fleet-config with ``/chief`` typed into it) whose PTY has since died — it was
  never renamed, so its row carries a project-derived name like
  ``fleet-config-79`` and the name scan cannot see it at all;
* any chief dead longer than :data:`src.board_state.STATE_STALE_AFTER` (24h) —
  the hook writer prunes its row on the same horizon, while the transcript
  stays on disk and ``claude --resume <id>`` still works by hand.

So the launcher **remembers** which conversation the chief is, instead of
re-deriving it from hook state every time. This module owns that memory and
nothing else: no chief policy lives here, only the sidecar's shape, its
expiry, and the degradation contract. Who writes it and how a reader ranks it
against the live rows is :mod:`app.webapp.routers.board`'s business.

Store: ``webapp/chief-pointer.json``, gitignored, atomically replaced — the
same machine-written-runtime-state neighbourhood as :mod:`src.audit`'s logs
and :mod:`src.jobs_history`'s run records, deliberately *not*
``config/webapp_config.json`` (hand-edited prefs, and the e2e isolation guard
asserts that file is byte-identical across a run).

Degradation contract, identical to every :mod:`src.board_state` reader:
missing, corrupt, or expired reads as ``{}`` — never an exception. A Resume
that can't read the pointer must degrade to the row scan, never fail.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from src._json_io import atomic_write_json
from src.board_state import _iso_z, _now, _parse_iso

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Module-level so tests redirect the whole module at a tmp_path instead of
# touching the real sidecar (the pattern src.jobs_history already uses).
CHIEF_POINTER_FILE = PROJECT_ROOT / "webapp" / "chief-pointer.json"

# Roberto's call (#675): long enough to survive a weekend or a session-host
# outage, short enough that a chief he moved on from weeks ago can't be
# resurrected. Deliberately far longer than board_state.STATE_STALE_AFTER's
# 24h — outliving that prune horizon is the entire point of persisting this.
POINTER_STALE_AFTER = timedelta(days=7)


def read_chief_pointer(
    path: Optional[Path] = None, *, now: Optional[datetime] = None
) -> Dict[str, Any]:
    """The remembered chief conversation, or ``{}``.

    Returns ``{"session_id", "transcript_path", "seen_at", "source"}`` with
    ``seen_at`` left as its stored ISO string. ``{}`` — never an exception —
    when the file is absent, unreadable, corrupt, missing a ``session_id``,
    older than :data:`POINTER_STALE_AFTER`, or points at a transcript that is
    no longer on disk.

    The transcript-existence check is part of *validity*, not a caller's job:
    a pointer whose conversation has been deleted is worth exactly as much as
    no pointer, and ``claude --resume`` would fail on it.
    """
    target = path or CHIEF_POINTER_FILE
    try:
        raw = json.loads(Path(target).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    session_id = str(raw.get("session_id") or "").strip()
    if not session_id:
        return {}
    seen_at = _parse_iso(raw.get("seen_at"))
    if seen_at is None or (now or _now()) - seen_at > POINTER_STALE_AFTER:
        return {}
    transcript_path = str(raw.get("transcript_path") or "").strip()
    if not transcript_path or not Path(transcript_path).exists():
        return {}
    return {
        "session_id": session_id,
        "transcript_path": transcript_path,
        "seen_at": str(raw.get("seen_at")),
        "source": str(raw.get("source") or ""),
    }


def write_chief_pointer(
    session_id: str,
    transcript_path: Any,
    source: str,
    path: Optional[Path] = None,
) -> None:
    """Remember ``session_id`` as the chief's conversation. Best-effort.

    ``source`` is a breadcrumb only (``self-heal`` | ``ensure-resume``) —
    nothing branches on it; it exists so a later reader of the file can tell
    how the launcher came to know this id.

    Never raises: this is called from a fire-and-forget task off a poll path,
    and a failed write must cost nothing more than the next Resume falling
    back to the row scan.
    """
    target = Path(path or CHIEF_POINTER_FILE)
    payload = {
        "session_id": str(session_id),
        "transcript_path": str(transcript_path or ""),
        "seen_at": _iso_z(_now()),
        "source": str(source),
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(target, payload)
    except OSError as exc:
        logger.debug("ℹ️ chief pointer write failed: %s", exc)
        return
    logger.info(
        "📌 chief pointer → %s (%s)", str(session_id)[:8], source,
    )
