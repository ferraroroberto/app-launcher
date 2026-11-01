"""Life OS conversations the zombie cleanup archived: list, read, restore.

    GET  /api/life-os/archived?q=…&skill=…                          → newest first
    GET  /api/life-os/archived/{day}/{skill}/{capture}/transcript   → Chat entries
    POST /api/life-os/archived/{day}/{skill}/{capture}/restore      → put it back

Everything is read from the backup (src/life_os/zombie_backup.py), never from
the live search index, so the capture pipeline keeps not seeing it. Gated like
the captures it holds (passkey, ``app/webapp/middleware/routes.py``). A URL's three
parts must name an entry ``find_backups`` itself lists before anything is read
or moved: nothing from the URL is joined onto a path unchecked.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, Tuple

from fastapi import APIRouter, HTTPException, Request

from src import audit
from src.life_os import capture as life_os_capture
from src.life_os import zombie_backup as life_os_zombie_backup
from src.webapp_config import WebappConfig

from app.webapp.routers._helpers import audit_off_loop, client_ip

logger = logging.getLogger(__name__)
router = APIRouter()

# A zombie is a greeting; the cap only stops a stray large file reaching the phone.
_MAX_TRANSCRIPT_BYTES = 1024 * 1024
_MAX_QUERY_CHARS = 200
# Two taps (or two devices) restoring at once would both pass restore_one's
# "nothing is there yet" checks before either moved a file.
_restore_lock = asyncio.Lock()


def _locate(day: str, skill: str, capture: str) -> Tuple[Path, Path]:
    """The backup root and the entry's folder, or 400/404 for anything it does not list."""
    root = life_os_zombie_backup.backup_root()
    try:
        found = life_os_zombie_backup.find_backups(root, f"{day}/{skill}/{capture}")
    except ValueError:
        raise HTTPException(status_code=400, detail="not an archived conversation")
    if (day, skill, capture) not in found:
        raise HTTPException(status_code=404, detail="archived conversation not found")
    return root, root / day / skill


@router.get("/api/life-os/archived")
async def archived_list(request: Request) -> Dict[str, Any]:
    """What the backup holds, optionally narrowed by ``q`` and ``skill``."""
    query = (request.query_params.get("q") or "").strip()
    skill = (request.query_params.get("skill") or "").strip()
    if len(query) > _MAX_QUERY_CHARS:
        raise HTTPException(status_code=400, detail="query too long")
    rows = await asyncio.to_thread(
        life_os_zombie_backup.list_archived, life_os_zombie_backup.backup_root(), query, skill
    )
    # The query text is the owner's own words: the hit count is all the trail needs.
    await audit_off_loop(
        audit.audit_event,
        "lifeos_archived_list", skill=skill, hits=len(rows), client=client_ip(request),
    )
    return {"archived": rows}


@router.get("/api/life-os/archived/{day}/{skill}/{capture}/transcript")
async def archived_transcript(day: str, skill: str, capture: str, request: Request) -> Dict[str, Any]:
    """One archived capture as Chat-pane entries, the shape ``/file/transcript`` answers."""
    _root, folder = _locate(day, skill, capture)
    path = folder / capture
    if not path.is_file() or path.is_symlink():
        raise HTTPException(status_code=404, detail="archived conversation not found")
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_TRANSCRIPT_BYTES + 1)
    except OSError:
        raise HTTPException(status_code=404, detail="archived conversation unreadable")
    parsed = life_os_capture.parse_capture(
        raw[:_MAX_TRANSCRIPT_BYTES].decode("utf-8", errors="replace")
    )
    await audit_off_loop(
        audit.audit_event,
        "lifeos_read", path=f"{day}/{skill}/{capture}", bytes=len(raw), view="archived",
        client=client_ip(request),
    )
    base = {"name": capture, "agent": parsed["agent"]}
    if not parsed["entries"]:
        return {**base, "available": False, "reason": "no_turns", "entries": []}
    return {
        **base, "available": True, "entries": parsed["entries"],
        "truncated": len(raw) > _MAX_TRANSCRIPT_BYTES,
    }


@router.post("/api/life-os/archived/{day}/{skill}/{capture}/restore")
async def restore_archived(day: str, skill: str, capture: str, request: Request) -> Dict[str, Any]:
    """Put one archived conversation back; 409 with the reason when it refuses."""
    cfg: WebappConfig = request.app.state.webapp_config
    async with _restore_lock:
        root, _folder = _locate(day, skill, capture)
        outcome = await asyncio.to_thread(
            life_os_zombie_backup.restore_one, cfg, root, day, skill, capture
        )
    await audit_off_loop(
        audit.audit_event,
        "lifeos_restore", path=f"{day}/{skill}/{capture}", status=outcome.status,
        client=client_ip(request),
    )
    if outcome.status == life_os_zombie_backup.STATUS_REFUSED:
        logger.info("ℹ️ Life OS archived restore refused: %s/%s: %s", skill, capture, outcome.reason)
        raise HTTPException(status_code=409, detail=outcome.reason)
    if outcome.status != life_os_zombie_backup.STATUS_RESTORED:
        raise HTTPException(status_code=500, detail=outcome.reason or "the restore did not finish")
    return {"status": outcome.status, "skill": skill, "capture": capture}
