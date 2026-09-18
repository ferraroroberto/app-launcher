"""Board tab — the fleet kanban's data plane (issues #300, #301, #302 / #164 / #399).

    GET  /api/board                       → the five computed columns (token-gated)
    POST /api/board/github/refresh        → run the gh searches now (token-gated)
    GET  /api/board/sessions/{sid}/exchange → last user↔assistant exchange
                                            (Tailscale + passkey — transcript text)
    POST /api/board/issues/start          → spawn /issue-start|yolo <N> in the
                                            issue's repo (Tailscale + passkey)
    POST /api/board/dispatch              → speak/type a goal into a fresh
                                            /issue-add|yolo session (Tailscale
                                            + passkey)

Split off a single-file god-router (issue #691, `/codebase-audit`), the way
``jobs.py`` and ``sessions.py`` already were: the fleet-chief lifecycle and its
settings routes (``/api/board/chief/*``) live in
:mod:`app.webapp.routers.board_chief`, mounted here via ``include_router`` so
``app/webapp/server.py`` still registers one ``board.router``. The spawn-then-
type mechanics both route modules share (readiness, quiescence, framing, the
per-launch model selector) live in :mod:`app.webapp.routers.board_spawn`, which
neither imports the other through.

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

Dispatch (#302) carries free text — the goal — so it can't use a positional
prompt at all. Instead it **spawns-then-types**: the session starts with only
the shared flags (no prompt), the endpoint polls until the agent has painted
its first output (``output_chars`` in the session dict) and its boot output
has gone quiet (the shared PTY-quiescence wait, #245/#549 — first paint alone
is not "input ready" and typing into a still-booting agent can swallow the
submitting CR, leaving the goal typed but never sent), then writes
``/issue-<mode> <goal>`` through the PTY input path inside bracketed-paste
framing with the submitting CR as its own second write (the #64/#166 framing
the reply proxy uses). The goal therefore never touches the unquoted
``cmd /c`` string. PTY-only: a remote session has no input path, and handing
free text to its command line is the exact injection this design avoids.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

from src import (
    active_issue_claims,
    agents,
    audit,
    board,
    github_client,
    quota_usage,
    session_client,
)
from src.board_exchange import resolve_exchange, unavailable
from src.launch_flags import build_claude_flags
from src.launcher import open_local_terminal_window, spawn_claude_session
from src.webapp_config import WebappConfig

from app.webapp.routers import board_chief
from app.webapp.routers._helpers import (
    audit_session_start_and_maybe_mirror,
    maybe_json,
    safe_int,
    spawn_launcher_session,
)
from app.webapp.routers.board_spawn import (
    _agent_and_flags,
    _read_live_sessions,
    _resolve_repo_entry,
    _safe_list_sessions,
    _type_into_session,
)

logger = logging.getLogger(__name__)
router = APIRouter()
router.include_router(board_chief.router)

_quota_refresh_gate = quota_usage.RefreshGate()
_quota_refresh_task: asyncio.Task[str] | None = None


def _github_section(snap: Dict[str, Any]) -> Dict[str, Any]:
    """``available`` says whether the GitHub-sourced columns are real (#910).

    ``False`` means the cache has never been filled in this process (a
    webapp restart empties it), so an empty Backlog/Done is *unknown*, not
    zero — the same ``available`` contract as ``sessions_state`` and
    ``active_issues``. ``error`` is orthogonal: set with ``available: True``
    it means the last refresh failed and the lists are the older good data.
    """
    fetched_at = snap.get("fetched_at")
    return {
        "available": fetched_at is not None,
        "fetched_at": fetched_at,
        "error": snap.get("error"),
    }


def _read_quota_lines(cfg: WebappConfig) -> List[Dict[str, Any]]:
    """Both heavy agents, always, independent of the current selection (#860)."""
    legacy_path = Path(cfg.rate_limits_file)
    return quota_usage.read_quota_lines(
        Path(cfg.claude_config_dir),
        legacy_path.parent,
        legacy_reader=lambda: board.read_rate_limits(legacy_path),
    )


def _refresh_finished(task: asyncio.Task[str]) -> None:
    global _quota_refresh_task
    _quota_refresh_gate.finish()
    _quota_refresh_task = None
    try:
        outcome = task.result()
    except Exception:  # pragma: no cover - asyncio owns unexpected task failures
        logger.exception("Codex quota refresh task failed")
        return
    logger.info("ℹ️ Codex quota refresh: %s", outcome)


def _maybe_refresh_codex(cfg: WebappConfig, rate_limits: Dict[str, Any]) -> None:
    """Schedule one canonical refresh without blocking a five-second poll."""
    global _quota_refresh_task
    if rate_limits.get("harness") != "codex" or rate_limits.get("state") == "available":
        return
    if not _quota_refresh_gate.begin():
        return
    try:
        _quota_refresh_task = asyncio.create_task(
            asyncio.to_thread(
                quota_usage.refresh_codex,
                Path(cfg.claude_config_dir),
                Path(cfg.rate_limits_file).parent,
            )
        )
    except Exception:
        _quota_refresh_gate.finish()
        raise
    _quota_refresh_task.add_done_callback(_refresh_finished)


def _refresh_codex_for_lines(
    cfg: WebappConfig, quota_lines: List[Dict[str, Any]]
) -> None:
    """Keep Codex fresh even while Claude is the selected harness (#860).

    The compact rows always show Codex, so the native collector can no
    longer be scheduled off the *selected* view alone — pointing the model
    picker at Claude used to leave Codex's row permanently stale.
    """
    for line in quota_lines:
        if line.get("harness") == "codex":
            _maybe_refresh_codex(cfg, line)
            return


def _mark_active_backlog(
    columns: Dict[str, List[Dict[str, Any]]],
    active_rows: Dict[str, Any],
    claim_states: Dict[str, str],
) -> None:
    """Annotate each backlog card from the shared ``repo#number`` mapping.

    ``claim_state`` is ``None`` (no claim) or the row owner's ``live`` /
    ``dead`` / ``unknown`` verdict (#948). A ``dead`` claim is a lane that
    is provably gone, so the card is not ``in_progress``; ``unknown`` (an
    owner-less row, or liveness that could not be read) keeps the pre-#948
    ``in_progress`` behaviour and the UI flags it unverified.
    """
    active_keys = {str(key).lower(): key for key in active_rows}
    for card in columns.get("backlog", []):
        repo = str(card.get("repo") or "").strip().lower()
        number = card.get("number")
        key = f"{repo}#{number}" if repo and isinstance(number, int) else ""
        row_key = active_keys.get(key)
        state = (
            claim_states.get(row_key, active_issue_claims.CLAIM_UNKNOWN)
            if row_key is not None
            else None
        )
        card["claim_state"] = state
        card["in_progress"] = state in (
            active_issue_claims.CLAIM_LIVE, active_issue_claims.CLAIM_UNKNOWN
        )


@router.get("/api/board")
async def get_board(request: Request) -> Dict[str, Any]:
    """The five columns + source health, cheap enough for the 5s poll."""
    cfg: WebappConfig = request.app.state.webapp_config

    active_issues_file = Path(cfg.sessions_state_file).with_name("active-issues.json")
    live_read, state, active_issues, job_cards, quota_lines = await asyncio.gather(
        asyncio.to_thread(_read_live_sessions, cfg.session_host_port),
        asyncio.to_thread(board.read_sessions_state, Path(cfg.sessions_state_file)),
        asyncio.to_thread(board.read_active_issues, active_issues_file),
        asyncio.to_thread(board.jobs_attention),
        asyncio.to_thread(_read_quota_lines, cfg),
    )
    github = github_client.snapshot()
    live, live_error = live_read

    live = board_chief._reconcile_chief_labels(live, state["rows"])
    # Claim owner liveness (#948): an unreadable session list is None, so the
    # contract answers unknown rather than calling every owner dead.
    claim_states = await asyncio.to_thread(
        active_issue_claims.classify_claims,
        active_issues["rows"],
        live_session_ids=(
            board._live_launcher_session_ids(live) if live_error is None else None
        ),
        fleet_config_dir=Path(cfg.claude_config_dir),
    )
    # Per-card transcript reads — unbounded in session count and re-run every
    # 5s while the Board is open, so it goes off the loop like the five
    # inputs above (#881).
    session_cards = await asyncio.to_thread(
        board.merge_sessions,
        live, state["rows"],
        active_issue_repos=board.active_issue_repos(active_issues["rows"], claim_states),
    )
    columns = board.build_board(session_cards, github, job_cards)
    _mark_active_backlog(columns, active_issues["rows"], claim_states)
    _refresh_codex_for_lines(cfg, quota_lines)

    return {
        "generated_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "columns": columns,
        "github": _github_section(github),
        # ``available: false`` means the session-host could not be read, so
        # Claude's turn and Your turn are unknown, not empty (#915) — the
        # same contract as ``github.available``.
        "live_sessions": {
            "available": live_error is None,
            "error": live_error,
        },
        "sessions_state": {
            "available": state["available"],
            "stale": state["stale"],
            "updated_at": state["updated_at"],
        },
        "active_issues": {
            "available": active_issues["available"],
            "updated_at": active_issues["updated_at"],
            "count": len(active_issues["rows"]),
        },
        "quota_lines": quota_lines,
    }


@router.get("/api/rate-limits")
async def get_rate_limits(request: Request) -> Dict[str, Any]:
    """The same quota rows as the Board tab, standalone from it.

    The Coding tab's Running-sessions header shows the same rows as the
    Board tab, but must not depend on the Board ever having been opened —
    ``GET /api/board``'s own quota read only happens as a side effect of
    that endpoint being polled, which fetchBoard() self-gates to "Board tab
    visible". This is the same cheap file read, exposed on its own route so
    any tab can poll it independently.

    Both heavy agents are always returned, in a fixed order (#860); the row
    set no longer depends on the caller's selected model.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    quota_lines = await asyncio.to_thread(_read_quota_lines, cfg)
    _refresh_codex_for_lines(cfg, quota_lines)
    return {"quota_lines": quota_lines}


@router.post("/api/board/github/refresh")
async def refresh_github(request: Request) -> Dict[str, Any]:
    """Run the fleet-wide gh searches now (subprocess-heavy, on demand only)."""
    cfg: WebappConfig = request.app.state.webapp_config
    snap = await asyncio.to_thread(github_client.refresh, cfg.github_owner)
    return _github_section(snap)


@router.get("/api/board/sessions/{sid}/exchange")
async def session_exchange(sid: str, request: Request) -> Dict[str, Any]:
    """Last user↔assistant exchange for a live session (Tailscale + passkey).

    Structured Claude/Codex history wins when it correlates safely. A Claude
    session whose state row names no transcript — the rowless window a
    ``/resume`` or ``/clear`` opens (#1023/#1027) — has its conversation
    correlated from the filesystem instead, reported as ``native_scan``
    because that match is inferred rather than exact. An unsupported agent,
    or a scan that refuses, falls back to the launcher's exact-id PTY
    capture + input audit, parsed on demand (never on the Board poll).
    Distinct unavailable reasons let the client separate true-empty from
    source error.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    live, state = await asyncio.gather(
        asyncio.to_thread(_safe_list_sessions, cfg.session_host_port),
        asyncio.to_thread(board.read_sessions_state, Path(cfg.sessions_state_file)),
    )
    session = next(
        (item for item in live if str(item.get("session_id")) == str(sid)), None
    )
    if session is None:
        return unavailable("session_not_found")
    row = board.state_row_for_session(live, state["rows"], sid)
    transcript = (row or {}).get("transcript_path")
    result = await asyncio.to_thread(
        resolve_exchange,
        session,
        transcript,
        audit.transcript_path(sid),
        audit.session_log_path(sid),
        live,
    )
    if result.get("source") == "native_scan":
        # The row named no transcript and the conversation was correlated
        # from the filesystem instead (#1027) — an inferred match, so it
        # leaves its own breadcrumb rather than passing for an exact one.
        logger.info(
            "ℹ️ Board exchange %s (%s) used a scanned native transcript; "
            "no state row named one",
            sid[:8], session.get("agent") or "claude",
        )
    elif result.get("source") == "launcher":
        logger.info(
            "ℹ️ Board exchange %s (%s) used exact-id launcher capture; "
            "native transcript unavailable",
            sid[:8], session.get("agent") or "claude",
        )
    elif not result.get("available"):
        logger.info(
            "ℹ️ Board exchange %s (%s) unavailable: %s",
            sid[:8], session.get("agent") or "claude", result.get("reason"),
        )
    return result


@router.post("/api/board/issues/start")
async def start_issue(request: Request) -> Dict[str, Any]:
    """One-tap ▶ Start / ⚡ YOLO on a backlog card (Tailscale + passkey, #301).

    Body: ``{"repo": str, "number": int, "mode": "start"|"yolo",
    "model": str, "rows": int, "cols": int, "title": str}``. The repo must
    resolve to a directory in the projects folder (the same live listing the
    Coding tab launches from); the prompt is built here as
    ``/issue-<mode> <number>`` — client text never reaches the command line.
    Spawns a streamed PTY session exactly like a Coding-tab launch (PC
    mirror rules included); the `/issue-*` skills themselves handle branch +
    worktree claiming inside the session.

    ``model`` (#505/#845) is the dispatch bar's provider-qualified selector
    applied to one-tap starts. Absent (stale-cache
    client) → the legacy persisted Coding model, exactly as before.

    The optional ``title`` (the Board card's issue title) auto-names the
    session after the issue (#467) via the #458 manual-override path, so it is
    recognizable in the Coding tab without waiting for the agent to self-name.
    The title is display data — it never reaches the command line.
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
    rows = safe_int(body, "rows", 40)
    cols = safe_int(body, "cols", 120)
    title = str(body.get("title") or "").strip()
    model = str(body.get("model") or "").strip().lower()
    if model:
        agent, base_flags = _agent_and_flags(cfg, model)
    else:
        agent, base_flags = "claude", build_claude_flags(cfg)

    entry = _resolve_repo_entry(cfg, repo)

    prompt = f"/issue-{mode} {number}"
    native_name_flags = agents.native_session_name_flags_for(agent, title)
    flags = " ".join(
        part for part in (base_flags, native_name_flags, f'"{prompt}"') if part
    )
    session, sid = await spawn_launcher_session(
        spawn_claude_session, cfg,
        project_dir=Path(entry.project_dir), name=entry.name,
        flags=flags, agent=agent, rows=rows, cols=cols,
    )
    await board_chief._mark_chief_managed(cfg, request, sid, entry.name, number)
    await audit_session_start_and_maybe_mirror(
        cfg, request, body,
        sid=sid, agent=agent, name=entry.name, project=entry.project_dir,
        skill=prompt, audit_mod=audit, mirror_fn=open_local_terminal_window,
    )
    # Auto-name the session after the issue title (#467): a Board-started
    # session is then recognizable in the Coding tab immediately, instead of
    # inheriting the first-prompt/OSC-derived default. Reuses the #458 manual
    # override (a launcher-side ``manual_title`` set, wins over the agent's
    # later self-naming). Best-effort — a rename failure must never fail an
    # otherwise-successful launch. No readiness wait needed: the rename is a
    # pure in-memory attribute set on the session record, never typed into
    # the PTY (the racy agent-native injection was removed in #555). Agents
    # with a verified spawn-time --name flag also receive the same safe title
    # above, so their native resume picker is synchronized from birth (#556).
    if sid and title:
        try:
            await asyncio.to_thread(
                session_client.rename, cfg.session_host_port, sid, title
            )
        except session_client.SessionHostError as exc:
            logger.warning(
                "⚠️ Board issue-start could not auto-name session %s: %s",
                sid[:8], exc,
            )
    return {"launched": prompt, "repo": entry.name, "session": session}


_DISPATCH_COMMANDS = {
    "add": "/issue-add",
    "build": "/issue-add now",
    "yolo": "/issue-yolo",
}


@router.post("/api/board/dispatch")
async def dispatch_goal(request: Request) -> Dict[str, Any]:
    """Free-text goal → a fresh ``/issue-*`` session (Tailscale + passkey, #302).

    Body: ``{"repo": str, "goal": str, "mode": "add"|"build"|"yolo",
    "model": "claude:<alias>"|"codex:<id>", "rows": int, "cols": int}``.
    Spawn-then-type per the module docstring: the goal rides the PTY input
    path, never the command line. The half-spawned session is killed on any
    failure past the spawn, so a timeout can't strand an orphan the user
    never asked for.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    body = await maybe_json(request)
    repo = str(body.get("repo") or "").strip()
    mode = str(body.get("mode") or "add").strip().lower()
    if mode not in _DISPATCH_COMMANDS:
        raise HTTPException(status_code=400, detail=f"unknown mode: {mode}")
    goal = body.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise HTTPException(
            status_code=400, detail="goal must be a non-empty string"
        )
    goal = goal.strip()
    # Per-launch model (#500) — sonnet default when absent (stale-cache
    # client). No positional prompt — see the module docstring.
    model = str(body.get("model") or "sonnet").strip().lower()
    agent, flags = _agent_and_flags(cfg, model)
    rows = safe_int(body, "rows", 40)
    cols = safe_int(body, "cols", 120)

    entry = _resolve_repo_entry(cfg, repo)

    session, sid = await spawn_launcher_session(
        spawn_claude_session, cfg,
        project_dir=Path(entry.project_dir), name=entry.name,
        flags=flags, agent=agent, rows=rows, cols=cols,
    )
    command = f"{_DISPATCH_COMMANDS[mode]} {goal}"
    await _type_into_session(cfg.session_host_port, sid, command)
    await board_chief._mark_chief_managed(cfg, request, sid, entry.name, 0)

    await audit_session_start_and_maybe_mirror(
        cfg, request, body,
        sid=sid, agent=agent, name=entry.name, project=entry.project_dir,
        skill=command, audit_mod=audit, mirror_fn=open_local_terminal_window,
    )
    return {"launched": command, "repo": entry.name, "session": session}
