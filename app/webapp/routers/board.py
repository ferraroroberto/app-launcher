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
its first output (``output_chars`` in the session dict), then writes
``/issue-<mode> <goal>`` through the PTY input path inside bracketed-paste
framing with the submitting CR as its own second write (the #64/#166 framing
the reply proxy uses). The goal therefore never touches the unquoted
``cmd /c`` string. PTY-only: a remote session has no input path, and handing
free text to its command line is the exact injection this design avoids.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from fastapi import APIRouter, HTTPException, Request

from src import agents, audit, board, github_client, session_client
from src.board_exchange import resolve_exchange, unavailable
from src.launcher import open_local_terminal_window, spawn_claude_session
from src.registry import AppEntry, live_claude_code_entries
from src.webapp_config import WebappConfig, build_claude_flags, build_codex_flags

from app.webapp.routers._helpers import (
    audit_session_start_and_maybe_mirror,
    maybe_json,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _resolve_repo_entry(cfg: WebappConfig, repo: str) -> AppEntry:
    """Resolve ``repo`` to a live claude-code entry, or 404.

    Shared by ``start_issue`` and ``dispatch_goal`` — both take a bare repo
    name from the client and need the same case-insensitive lookup against
    the live projects-folder listing.
    """
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
    return entry


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


def _rate_limits_section(rate_limits: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "available": rate_limits["available"],
        "stale": rate_limits["stale"],
        "updated_at": rate_limits["updated_at"],
        "five_hour": rate_limits["five_hour"],
        "seven_day": rate_limits["seven_day"],
    }


@router.get("/api/board")
async def get_board(request: Request) -> Dict[str, Any]:
    """The five columns + source health, cheap enough for the 5s poll."""
    cfg: WebappConfig = request.app.state.webapp_config

    live, state, job_cards, rate_limits = await asyncio.gather(
        asyncio.to_thread(_safe_list_sessions, cfg.session_host_port),
        asyncio.to_thread(board.read_sessions_state, Path(cfg.sessions_state_file)),
        asyncio.to_thread(board.jobs_attention),
        asyncio.to_thread(board.read_rate_limits, Path(cfg.rate_limits_file)),
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
        "rate_limits": _rate_limits_section(rate_limits),
    }


@router.get("/api/rate-limits")
async def get_rate_limits(request: Request) -> Dict[str, Any]:
    """Claude 5h/7d usage % (issue #326), standalone from the Board tab.

    The Coding tab's Running-sessions header shows the same usage badges as
    the Board tab, but must not depend on the Board ever having been opened
    — ``GET /api/board``'s own rate-limits read only happens as a side
    effect of that endpoint being polled, which fetchBoard() self-gates to
    "Board tab visible". This is the same cheap one-file read, exposed on
    its own route so any tab can poll it independently.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    rate_limits = await asyncio.to_thread(
        board.read_rate_limits, Path(cfg.rate_limits_file)
    )
    return _rate_limits_section(rate_limits)


@router.post("/api/board/github/refresh")
async def refresh_github(request: Request) -> Dict[str, Any]:
    """Run the fleet-wide gh searches now (subprocess-heavy, on demand only)."""
    cfg: WebappConfig = request.app.state.webapp_config
    snap = await asyncio.to_thread(github_client.refresh, cfg.github_owner)
    return _github_section(snap)


@router.get("/api/board/sessions/{sid}/exchange")
async def session_exchange(sid: str, request: Request) -> Dict[str, Any]:
    """Last user↔assistant exchange for a live session (Tailscale + passkey).

    Structured Claude/Codex history wins when it correlates safely. A missing
    hook JSONL or unsupported agent falls back to the launcher's exact-id PTY
    capture + input audit, parsed on demand (never on the Board poll). Distinct
    unavailable reasons let the client separate true-empty from source error.
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
    )
    if result.get("source") == "launcher":
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

    ``model`` (#505) is the dispatch bar's selector applied to one-tap
    starts: the #500 values override the shared Coding model (gpt5.6 →
    Codex, which takes the same positional prompt). Absent (stale-cache
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
    rows = int(body.get("rows") or 40)
    cols = int(body.get("cols") or 120)
    title = str(body.get("title") or "").strip()
    model = str(body.get("model") or "").strip().lower()
    if model:
        agent, base_flags = _agent_and_flags(cfg, model)
    else:
        agent, base_flags = "claude", build_claude_flags(cfg)

    entry = _resolve_repo_entry(cfg, repo)

    prompt = f"/issue-{mode} {number}"
    flags = f'{base_flags} "{prompt}"'
    try:
        session = await asyncio.to_thread(
            spawn_claude_session,
            Path(entry.project_dir),
            entry.name,
            flags,
            cfg.session_host_port,
            "pty",
            agent,
            rows,
            cols,
            history_lines=cfg.terminal_history_lines,
        )
    except session_client.SessionHostError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    sid = str(session.get("session_id") or "")
    await audit_session_start_and_maybe_mirror(
        cfg, request, body,
        sid=sid, agent=agent, name=entry.name, project=entry.project_dir,
        skill=prompt, audit_mod=audit, mirror_fn=open_local_terminal_window,
    )
    # Auto-name the session after the issue title (#467): a Board-started
    # session is then recognizable in the Coding tab immediately, instead of
    # inheriting the first-prompt/OSC-derived default. Reuses the #458 manual
    # override (wins over the agent's later self-naming). Best-effort — a
    # rename failure must never fail an otherwise-successful launch.
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


# Dispatch readiness (#302): how long to wait for the freshly spawned agent
# to paint its first output before typing into it, the settle after that
# first paint, and the fixed grace for a session-host old enough to not
# report ``output_chars`` yet. Module-level so tests can patch them tiny.
DISPATCH_READY_CAP_S = 15.0
DISPATCH_SETTLE_S = 2.0
DISPATCH_POLL_S = 0.25
DISPATCH_LEGACY_GRACE_S = 5.0

_DISPATCH_COMMANDS = {
    "add": "/issue-add",
    "build": "/issue-add now",
    "yolo": "/issue-yolo",
}

# Dispatch model selector (#500): three Claude tiers (each a valid
# ``build_claude_flags`` override) plus "gpt5.6", which spawns a Codex CLI
# session with the shared Coding-tab flags instead — Codex has no per-model
# flag, so "gpt5.6" just means "the account's default model at the
# configured effort", same as the Coding tab's Codex button.
_DISPATCH_CLAUDE_MODELS = ("sonnet", "opus", "fable")
_DISPATCH_CODEX_MODEL = "gpt5.6"


def _agent_and_flags(cfg: WebappConfig, model: str) -> Tuple[str, str]:
    """Validated ``(agent, flags)`` for a Board per-launch ``model`` (#500/#505).

    The Claude tiers force ``--model`` like the Life OS tab's toggle (#102);
    ``gpt5.6`` selects Codex with the Coding tab's shared flags instead
    (``apps.py``'s exact launch shape). The ``is_installed`` check is the
    same defence-in-depth 400 as ``apps.py`` — Board launches bypass the
    Coding tab's already-disabled button.
    """
    if model not in _DISPATCH_CLAUDE_MODELS and model != _DISPATCH_CODEX_MODEL:
        raise HTTPException(status_code=400, detail=f"unknown model: {model}")
    if model == _DISPATCH_CODEX_MODEL:
        if not agents.is_installed("codex"):
            raise HTTPException(
                status_code=400,
                detail=f"{agents.AGENTS['codex'].label} is not installed",
            )
        return "codex", build_codex_flags(cfg)
    return "claude", build_claude_flags(cfg, model)


async def _await_dispatch_ready(port: int, sid: str) -> None:
    """Block until the spawned agent is safe to type into, or raise 504.

    Ready = alive **and** first output seen (``output_chars > 0``), then a
    short settle so the TUI has its input box up. A session dict without
    ``output_chars`` means the live session-host predates #302 — degrade to
    a fixed grace (⚠️ logged) rather than refusing, so dispatch works until
    the host's next restart picks up the real probe. Never returns for a
    dead session: typing into a dead PTY is the one forbidden outcome.
    """
    deadline = time.monotonic() + DISPATCH_READY_CAP_S
    legacy = False
    while True:
        info = await asyncio.to_thread(session_client.get_session, port, sid)
        if not info.get("alive"):
            raise HTTPException(
                status_code=504, detail="session died during startup"
            )
        chars = info.get("output_chars")
        if chars is None:
            legacy = True
            break
        if chars > 0:
            break
        if time.monotonic() >= deadline:
            raise HTTPException(
                status_code=504,
                detail=(
                    f"session produced no output within "
                    f"{DISPATCH_READY_CAP_S:.0f}s"
                ),
            )
        await asyncio.sleep(DISPATCH_POLL_S)
    if legacy:
        logger.warning(
            "⚠️ session-host predates output_chars — dispatching after a "
            f"fixed {DISPATCH_LEGACY_GRACE_S:.0f}s grace"
        )
        await asyncio.sleep(DISPATCH_LEGACY_GRACE_S)
    else:
        await asyncio.sleep(DISPATCH_SETTLE_S)
    info = await asyncio.to_thread(session_client.get_session, port, sid)
    if not info.get("alive"):
        raise HTTPException(status_code=504, detail="session died during startup")


@router.post("/api/board/dispatch")
async def dispatch_goal(request: Request) -> Dict[str, Any]:
    """Free-text goal → a fresh ``/issue-*`` session (Tailscale + passkey, #302).

    Body: ``{"repo": str, "goal": str, "mode": "add"|"build"|"yolo",
    "model": "sonnet"|"opus"|"fable"|"gpt5.6", "rows": int, "cols": int}``.
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
    rows = int(body.get("rows") or 40)
    cols = int(body.get("cols") or 120)

    entry = _resolve_repo_entry(cfg, repo)

    try:
        session = await asyncio.to_thread(
            spawn_claude_session,
            Path(entry.project_dir),
            entry.name,
            flags,
            cfg.session_host_port,
            "pty",
            agent,
            rows,
            cols,
            history_lines=cfg.terminal_history_lines,
        )
    except session_client.SessionHostError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    sid = str(session.get("session_id") or "")
    command = f"{_DISPATCH_COMMANDS[mode]} {goal}"
    try:
        await _await_dispatch_ready(cfg.session_host_port, sid)
        # Bracketed-paste framing keeps the goal one atomic paste (no
        # per-keystroke TUI interpretation) and routes it through the
        # first-prompt title capture (#266); the CR is its own second
        # write so the paste-end marker can't swallow it (#64/#166).
        await asyncio.to_thread(
            session_client.send_input,
            cfg.session_host_port,
            sid,
            "\x1b[200~" + command + "\x1b[201~",
        )
        await asyncio.to_thread(
            session_client.send_input, cfg.session_host_port, sid, "\r"
        )
    except (HTTPException, session_client.SessionHostError) as exc:
        try:
            await asyncio.to_thread(
                session_client.stop, cfg.session_host_port, sid, "kill"
            )
        except session_client.SessionHostError:
            pass
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=exc.status, detail=str(exc))

    await audit_session_start_and_maybe_mirror(
        cfg, request, body,
        sid=sid, agent=agent, name=entry.name, project=entry.project_dir,
        skill=command, audit_mod=audit, mirror_fn=open_local_terminal_window,
    )
    return {"launched": command, "repo": entry.name, "session": session}
