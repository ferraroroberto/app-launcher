"""Board tab — the fleet kanban's data plane (issues #300, #301 / #164).

    GET  /api/board                       → the four computed columns (token-gated)
    POST /api/board/github/refresh        → run the gh searches now (token-gated)
    GET  /api/board/sessions/{sid}/exchange → last user↔assistant exchange
                                            (Tailscale + passkey — transcript text)
    POST /api/board/issues/start          → spawn /issue-start|yolo <N> in the
                                            issue's repo (Tailscale + passkey)

``GET /api/board`` is the 5s poll target, so it does only cheap work: the live
session list from the session-host, one state-file read, one jobs-runs walk
(all in worker threads, gathered concurrently) and a pure memory read of the
GitHub cache. The ``gh`` subprocesses run **only** inside the explicit refresh
endpoint — the exact on-demand contract of the Coding tab's ⎇ git-status
button. Column assembly is pure logic in :mod:`src.board`.

The board + refresh routes are read-only repo/session metadata — the same gate
class as ``GET /api/claude-code/sessions`` (bearer token, no passkey). The
drill-down exchange and issue-start routes (#301) are terminal-grade and get
the passkey gate in ``middleware._terminal_guard_level``; the reply proxy
lives beside its session siblings in ``routers/sessions.py``.

Issue-start is injection-safe by construction: the positional prompt is built
**server-side** as ``/issue-<mode> <N>`` with ``mode`` allowlisted and ``N``
int-validated, so the string that reaches the session-host's unquoted
``cmd /c`` line can never contain a metacharacter.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

from src import audit, board, github_client, session_client
from src.launcher import open_local_terminal_window, spawn_claude_session
from src.registry import live_claude_code_entries
from src.webapp_config import WebappConfig, build_claude_flags

from app.webapp.routers._helpers import (
    cert_present,
    client_ip,
    maybe_json,
    should_mirror_to_pc,
)

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


@router.get("/api/board/sessions/{sid}/exchange")
async def session_exchange(sid: str, request: Request) -> Dict[str, Any]:
    """Last user↔assistant exchange for a live session (Tailscale + passkey).

    The session-host knows nothing about transcripts — the path comes from
    the hook state row the board's merge assigns to this session (same
    cwd-join, same tiebreak, via :func:`board.state_row_for_session`), so the
    drawer always shows the conversation the card's status came from. No row
    or no transcript → ``{"available": False}``, never an error.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    live, state = await asyncio.gather(
        asyncio.to_thread(_safe_list_sessions, cfg.session_host_port),
        asyncio.to_thread(board.read_sessions_state, Path(cfg.sessions_state_file)),
    )
    row = board.state_row_for_session(live, state["rows"], sid)
    transcript = (row or {}).get("transcript_path")
    if not transcript:
        return {"available": False, "user": None, "assistant": None}
    return await asyncio.to_thread(board.last_exchange, transcript)


@router.post("/api/board/issues/start")
async def start_issue(request: Request) -> Dict[str, Any]:
    """One-tap ▶ Start / ⚡ YOLO on a backlog card (Tailscale + passkey, #301).

    Body: ``{"repo": str, "number": int, "mode": "start"|"yolo",
    "rows": int, "cols": int}``. The repo must resolve to a directory in the
    projects folder (the same live listing the Coding tab launches from);
    the prompt is built here as ``/issue-<mode> <number>`` — client text
    never reaches the command line. Spawns a streamed PTY session exactly
    like a Coding-tab launch (PC mirror rules included); the `/issue-*`
    skills themselves handle branch + worktree claiming inside the session.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    body = await maybe_json(request)
    repo = str(body.get("repo") or "").strip()
    mode = str(body.get("mode") or "start").strip().lower()
    if mode not in ("start", "yolo"):
        raise HTTPException(status_code=400, detail=f"unknown mode: {mode}")
    try:
        number = int(body.get("number"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="number must be an integer")
    if number <= 0:
        raise HTTPException(status_code=400, detail="number must be positive")
    rows = int(body.get("rows") or 40)
    cols = int(body.get("cols") or 120)

    entries = live_claude_code_entries(
        Path(cfg.projects_dir), list(cfg.projects_ignore)
    )
    entry = next(
        (e for e in entries if e.name.lower() == repo.lower()), None
    )
    if entry is None or not entry.project_dir:
        raise HTTPException(
            status_code=404, detail=f"repo not in the projects folder: {repo}"
        )

    prompt = f"/issue-{mode} {number}"
    flags = f'{build_claude_flags(cfg)} "{prompt}"'
    try:
        session = await asyncio.to_thread(
            spawn_claude_session,
            Path(entry.project_dir),
            entry.name,
            flags,
            cfg.session_host_port,
            "pty",
            "claude",
            rows,
            cols,
        )
    except session_client.SessionHostError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    sid = str(session.get("session_id") or "")
    audit.audit_event(
        "session_start",
        session=sid,
        agent="claude",
        name=entry.name,
        project=entry.project_dir,
        skill=prompt,
        client=client_ip(request),
    )
    audit.session_log(
        sid, "start", agent="claude", name=entry.name,
        project=entry.project_dir, skill=prompt,
    )
    if should_mirror_to_pc(cfg.claude_show_local_window, request, body):
        scheme = "https" if cert_present() else "http"
        pc_url = f"{scheme}://127.0.0.1:{cfg.port}/?terminal={sid}"
        asyncio.create_task(
            asyncio.to_thread(open_local_terminal_window, pc_url, sid)
        )
    return {"launched": prompt, "repo": entry.name, "session": session}
