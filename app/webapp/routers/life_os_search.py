"""Life OS cross-skill conversation search.

    GET  /api/life-os/conversations/search     → ranked cross-skill search
                                                 (Tailscale + passkey)

Mounted on ``life_os.router`` beside :mod:`app.webapp.routers.life_os_conversations`,
whose row shaping it reuses so a search hit renders like an index entry.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request

from src import audit
from src.life_os_index import search_cli
from src.subprocess_flags import NO_WINDOW
from src.webapp_config import WebappConfig

from app.webapp.routers._helpers import audit_off_loop, client_ip
from app.webapp.routers.life_os_conversations import _conversation_rows

logger = logging.getLogger(__name__)
router = APIRouter()

_SEARCH_TIMEOUT_S = 15
# A query long enough to be a paste accident rather than a search; the CLI is
# invoked with an argv list (never a shell), so this is a sanity cap, not the
# injection guard.
_MAX_QUERY_CHARS = 200
_SEARCH_LIMIT_DEFAULT = 20
_SEARCH_LIMIT_MAX = 100


def _search_unavailable(reason: str) -> Dict[str, Any]:
    """The degraded-but-honest search reply.

    ``reason`` is a short, already-sanitised sentence for the phone — never
    an exception string, a path, or a stderr dump (those go to the log). The
    UI renders it as "search unavailable", not an error toast.
    """
    return {"available": False, "reason": reason, "results": []}


def _search_limit(raw: Optional[str]) -> int:
    """Clamp the caller's ``limit`` into a sane range."""
    try:
        limit = int(raw) if raw else _SEARCH_LIMIT_DEFAULT
    except (TypeError, ValueError):
        return _SEARCH_LIMIT_DEFAULT
    return max(1, min(limit, _SEARCH_LIMIT_MAX))


@router.get("/api/life-os/conversations/search")
async def search_conversations(request: Request) -> Dict[str, Any]:
    """Ranked conversation search across every skill (Tailscale + passkey).

    Shells out to fleet-config's ``conversation_search`` CLI — the launcher
    owns none of the ranking: that is an FTS5 index over the digests *and*
    the full capture text, so an offhand detail no digest mentions still
    finds the right conversation. ``--cwd`` (not ``--project``) resolves the
    project from the configured ``life_os_dir``, so a non-default checkout
    still works.

    Every failure mode — no fleet-config, no database yet, a non-zero exit, a
    timeout, unreadable output — degrades to ``available: false`` with a
    short reason. This endpoint never 500s and never surfaces infrastructure
    detail to the phone; the detail goes to the log.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    query = (request.query_params.get("q") or "").strip()
    skill = (request.query_params.get("skill") or "").strip()
    limit = _search_limit(request.query_params.get("limit"))
    # An empty box is not a failure and not a search — answer it without
    # spawning anything, so typing-then-clearing costs nothing.
    if not query:
        return {"available": True, "query": "", "skill": skill, "results": []}
    if len(query) > _MAX_QUERY_CHARS:
        raise HTTPException(status_code=400, detail="query too long")
    reply = await run_conversation_search(cfg, query, skill, limit)
    if not reply["available"]:
        return reply
    # Audited like every other private-content read, but the query text is
    # deliberately not recorded: it is the user's own words about their own
    # life, and the hit count is all an audit trail needs to be useful here.
    await audit_off_loop(
        audit.audit_event,
        "lifeos_search", skill=skill, hits=len(reply["results"]), client=client_ip(request)
    )
    return reply


async def run_conversation_search(
    cfg: WebappConfig, query: str, skill: str, limit: int,
) -> Dict[str, Any]:
    """Run the search CLI for a non-empty, length-checked ``query``; the route's
    reply shape, unaudited. ``skill`` (may be ``""``) narrows to one skill's
    hits. Shared with the Telegram relay's find (#1639), which audits itself.
    """
    life_os_dir = Path(cfg.life_os_dir)
    cli = search_cli(cfg)
    if cli is None:
        return _search_unavailable("conversation search is not installed")

    argv = [
        *cli, "--cwd", str(life_os_dir), "--query", query,
        "--limit", str(limit), "--json",
    ]
    if skill:
        argv.extend(["--skill", skill])
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_SEARCH_TIMEOUT_S,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("⚠️ conversation search did not run: %s", exc)
        return _search_unavailable("search is not responding")
    if proc.returncode != 0:
        logger.warning(
            "⚠️ conversation search exited %s: %s",
            proc.returncode, (proc.stderr or "").strip()[:400],
        )
        return _search_unavailable("no conversation index has been built yet")
    try:
        rows = json.loads(proc.stdout or "[]")
    except ValueError:
        logger.warning("⚠️ conversation search returned unparseable JSON")
        return _search_unavailable("search returned an unreadable result")
    if not isinstance(rows, list):
        return _search_unavailable("search returned an unreadable result")

    results = await asyncio.to_thread(
        _conversation_rows, cfg,
        [row for row in rows if isinstance(row, dict) and (not skill or row.get("skill") == skill)],
    )
    return {
        "available": True,
        "query": query,
        "skill": skill,
        "results": results,
    }
