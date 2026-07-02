"""Board tab — the fleet kanban's read-only data plane (issue #300 / #164).

    GET  /api/board                 → the four computed columns (token-gated)
    POST /api/board/github/refresh  → run the gh searches now (token-gated)

``GET /api/board`` is the 5s poll target, so it does only cheap work: the live
session list from the session-host, one state-file read, one jobs-runs walk
(all in worker threads, gathered concurrently) and a pure memory read of the
GitHub cache. The ``gh`` subprocesses run **only** inside the explicit refresh
endpoint — the exact on-demand contract of the Coding tab's ⎇ git-status
button. Column assembly is pure logic in :mod:`src.board`.

Both routes are read-only repo/session metadata — the same gate class as
``GET /api/claude-code/sessions`` (bearer token, no passkey). Anything that
writes to a PTY or spawns a session is Step 2/3 territory (#301, #302) and
will be passkey-gated there.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Request

from src import board, github_client, session_client
from src.webapp_config import WebappConfig

logger = logging.getLogger(__name__)
router = APIRouter()


def _safe_list_sessions(port: int) -> List[Dict[str, Any]]:
    """Live sessions, or [] when the session-host is down — the board must
    keep rendering GitHub + jobs cards regardless (#164 degradation)."""
    try:
        return session_client.list_sessions(port)
    except session_client.SessionHostError as exc:
        logger.debug(f"board: session list failed: {exc}")
        return []


def _github_section(snap: Dict[str, Any]) -> Dict[str, Any]:
    return {"fetched_at": snap.get("fetched_at"), "error": snap.get("error")}


@router.get("/api/board")
async def get_board(request: Request) -> Dict[str, Any]:
    """The four columns + source health, cheap enough for the 5s poll."""
    cfg: WebappConfig = request.app.state.webapp_config

    live, state, job_cards = await asyncio.gather(
        asyncio.to_thread(_safe_list_sessions, cfg.session_host_port),
        asyncio.to_thread(board.read_sessions_state, Path(cfg.sessions_state_file)),
        asyncio.to_thread(board.jobs_attention),
    )
    github = github_client.snapshot()

    session_cards = board.merge_sessions(live, state["rows"])
    columns = board.build_board(session_cards, github, job_cards)

    return {
        "generated_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "columns": columns,
        "github": _github_section(github),
        "sessions_state": {
            "available": state["available"],
            "stale": state["stale"],
            "updated_at": state["updated_at"],
        },
    }


@router.post("/api/board/github/refresh")
async def refresh_github(request: Request) -> Dict[str, Any]:
    """Run the fleet-wide gh searches now (subprocess-heavy, on demand only)."""
    cfg: WebappConfig = request.app.state.webapp_config
    snap = await asyncio.to_thread(github_client.refresh, cfg.github_owner)
    return _github_section(snap)
