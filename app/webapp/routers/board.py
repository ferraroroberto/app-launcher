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
    POST /api/board/chief/ensure          → spawn the fleet chief if absent
                                            (?fresh=1 kills + respawns) —
                                            Tailscale + passkey
    GET  /api/board/chief/settings        → chief settings block (also read
                                            by the /chief skill over loopback)
    PUT  /api/board/chief/settings        → persist chief settings + resync
                                            the daily respawn job

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
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from fastapi import APIRouter, HTTPException, Request

from src import agents, audit, board, github_client, session_client
from src import jobs as jobs_mod
from src import jobs_config
from src.board_exchange import resolve_exchange, unavailable
from src.launcher import open_local_terminal_window, spawn_claude_session
from src.registry import AppEntry, live_claude_code_entries
from src.webapp_config import (
    WebappConfig,
    build_claude_flags,
    build_codex_flags,
    update_webapp_config,
)

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


def _mark_active_backlog(
    columns: Dict[str, List[Dict[str, Any]]], active_rows: Dict[str, Any]
) -> None:
    """Annotate each backlog card from the shared ``repo#number`` mapping."""
    active_keys = {str(key).lower() for key in active_rows}
    for card in columns.get("backlog", []):
        repo = str(card.get("repo") or "").strip().lower()
        number = card.get("number")
        key = f"{repo}#{number}" if repo and isinstance(number, int) else ""
        card["in_progress"] = key in active_keys


@router.get("/api/board")
async def get_board(request: Request) -> Dict[str, Any]:
    """The five columns + source health, cheap enough for the 5s poll."""
    cfg: WebappConfig = request.app.state.webapp_config

    active_issues_file = Path(cfg.sessions_state_file).with_name("active-issues.json")
    live, state, active_issues, job_cards, rate_limits = await asyncio.gather(
        asyncio.to_thread(_safe_list_sessions, cfg.session_host_port),
        asyncio.to_thread(board.read_sessions_state, Path(cfg.sessions_state_file)),
        asyncio.to_thread(board.read_active_issues, active_issues_file),
        asyncio.to_thread(board.jobs_attention),
        asyncio.to_thread(board.read_rate_limits, Path(cfg.rate_limits_file)),
    )
    github = github_client.snapshot()

    session_cards = board.merge_sessions(live, state["rows"])
    columns = board.build_board(session_cards, github, job_cards)
    _mark_active_backlog(columns, active_issues["rows"])

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
        "active_issues": {
            "available": active_issues["available"],
            "updated_at": active_issues["updated_at"],
            "count": len(active_issues["rows"]),
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
    # rename failure must never fail an otherwise-successful launch. Waits
    # for the same readiness signal the goal-dispatch path already trusts
    # (_await_dispatch_ready/_await_pty_quiescent, #302/#549) before typing:
    # a rename fired the instant the session spawns races the agent's own
    # boot output (handshake, banner) and gets swallowed (#553) — the same
    # typed-into-a-not-ready-PTY race #551 fixed on the too-*late* side.
    if sid and title:
        try:
            await _await_dispatch_ready(cfg.session_host_port, sid)
            await _await_pty_quiescent(cfg.session_host_port, sid)
            await asyncio.to_thread(
                session_client.rename, cfg.session_host_port, sid, title
            )
        except (HTTPException, session_client.SessionHostError) as exc:
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

# PTY input-quiescence (#245 review, generalized #549): first-paint + a fixed
# settle is NOT always "input ready" for a fresh --remote-control agent — a
# CR typed while boot output (handshake, banner) is still growing gets
# swallowed, leaving the typed text sitting unsubmitted (observed on-device
# 2026-07-18, first on the chief's own rename, then on a chief-dispatched
# /issue-add worker left idle with its goal typed but never submitted). Stable
# output for PTY_QUIESCENT_STABLE_S is the strongest cheap signal the prompt
# has settled; cap and proceed best-effort rather than failing the spawn.
# Module-level so tests can patch them tiny.
PTY_QUIESCENT_STABLE_S = 2.0
PTY_QUIESCENT_CAP_S = 30.0
PTY_QUIESCENT_POLL_S = 0.5

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


async def _await_pty_quiescent(port: int, sid: str) -> None:
    """Wait until the session's output stops growing (best-effort).

    ``output_chars > 0`` (what :func:`_await_dispatch_ready` checks) means
    "painted something", not "input box live" — a fresh --remote-control
    agent keeps booting (handshake, banner) well past first paint, and a CR
    typed in that window is swallowed, merging the typed text with whatever
    is typed next (#245 review; generalized to every typed submission in
    #549 after the same race hit a chief-dispatched worker). Stable output
    for ``PTY_QUIESCENT_STABLE_S`` is the strongest cheap signal the prompt
    is settled. On cap: proceed — typing slightly early degrades, failing
    the spawn is worse. A session dict without ``output_chars`` or a dead
    session-host is a legacy/gone host — nothing to lean on, so return
    immediately and let the caller's own probe surface the real state.
    """
    deadline = time.monotonic() + PTY_QUIESCENT_CAP_S
    last_chars = -1
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        try:
            info = await asyncio.to_thread(session_client.get_session, port, sid)
        except session_client.SessionHostError:
            return
        chars = info.get("output_chars")
        if chars is None:
            return
        if chars != last_chars:
            last_chars = chars
            stable_since = time.monotonic()
        elif time.monotonic() - stable_since >= PTY_QUIESCENT_STABLE_S:
            return
        await asyncio.sleep(PTY_QUIESCENT_POLL_S)
    logger.warning(
        "⚠️ PTY %s boot never went quiet within %.0fs — typing anyway",
        sid[:8], PTY_QUIESCENT_CAP_S,
    )


async def _type_into_session(port: int, sid: str, command: str) -> None:
    """Await readiness + quiescence, then type ``command`` into the PTY.

    Bracketed-paste framing keeps the text one atomic paste (no
    per-keystroke TUI interpretation) and routes it through the
    first-prompt title capture (#266); the CR is its own second write so
    the paste-end marker can't swallow it (#64/#166). The quiescence wait
    (#549) guards against typing while the agent's boot output is still
    growing, which can swallow that submitting CR — first-paint alone is
    not enough. On any failure past the spawn the half-spawned session is
    killed, so a timeout can't strand an orphan the user never asked for.
    Shared by dispatch (#302) and the chief ensure (#245) so the timing
    rules stay single-sourced instead of drifting between call sites.
    """
    try:
        await _await_dispatch_ready(port, sid)
        await _await_pty_quiescent(port, sid)
        await asyncio.to_thread(
            session_client.send_input, port, sid,
            "\x1b[200~" + command + "\x1b[201~",
        )
        await asyncio.to_thread(session_client.send_input, port, sid, "\r")
    except (HTTPException, session_client.SessionHostError) as exc:
        try:
            await asyncio.to_thread(session_client.stop, port, sid, "kill")
        except session_client.SessionHostError:
            pass
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=exc.status, detail=str(exc))


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
    await _type_into_session(cfg.session_host_port, sid, command)

    await audit_session_start_and_maybe_mirror(
        cfg, request, body,
        sid=sid, agent=agent, name=entry.name, project=entry.project_dir,
        skill=command, audit_mod=audit, mirror_fn=open_local_terminal_window,
    )
    return {"launched": command, "repo": entry.name, "session": session}


# --------------------------------------------------------- fleet chief (#245)
# The standing conversational orchestrator behind the Board's chat mode. The
# chief is a normal PTY session (label="chief") spawned in the fleet-config
# checkout — that cwd is what loads the fleet-only /chief skill tier and
# keeps app-launcher's own project context out of it. The server ships zero
# chief prose: the spawn types only "/chief", so the brain stays versioned
# in fleet-config. Three triggers hit the same ensure endpoint: the first
# chat-mode message (lazy), the registered chief-daily-respawn job
# (?fresh=1, context hygiene), and the manual Start button.

_CHIEF_REPO = "fleet-config"
_CHIEF_LABEL = "chief"
_CHIEF_COMMAND = "/chief"
_CHIEF_TITLE = "chief"
_CHIEF_JOB_ID = "chief-daily-respawn"
# How long a fresh respawn waits for the old chief's graceful stop before
# escalating to kill. Module-level so tests can patch it tiny.
CHIEF_STOP_WAIT_S = 8.0
CHIEF_STOP_POLL_S = 0.25

# Two concurrent ensures (e.g. a chat send racing the daily job) must not
# double-spawn; the lock serializes the check-then-spawn window.
_CHIEF_ENSURE_LOCK = asyncio.Lock()


def _find_chief(live: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The alive chief PTY session, or {}. Matches the ``label`` tag with a
    ``name`` fallback so a legacy session-host that didn't echo ``label``
    still can't be double-spawned."""
    for sess in live:
        if not sess.get("alive") or sess.get("kind") != "pty":
            continue
        if sess.get("label") == _CHIEF_LABEL or sess.get("name") == _CHIEF_LABEL:
            return sess
    return {}


def _chief_settings_payload(cfg: WebappConfig) -> Dict[str, Any]:
    return {
        "model": cfg.chief_model,
        "respawn_enabled": cfg.chief_respawn_enabled,
        "respawn_at": cfg.chief_respawn_at,
        "worker_cap": cfg.chief_worker_cap,
    }


def _sync_chief_respawn_job(enabled: bool, at: str) -> str:
    """Point the registered chief-daily-respawn job at the new time, or park
    it. Returns a warning string ("" when fine) instead of raising — the
    settings save must persist even when the job isn't registered yet.
    Runs in a worker thread (jobs I/O + schtasks are blocking)."""
    jobs_cfg = jobs_config.load_jobs()
    job = jobs_config.get_by_id(jobs_cfg, _CHIEF_JOB_ID)
    if job is None:
        return (
            f"{_CHIEF_JOB_ID} job not registered — copy it from "
            "config/jobs.sample.json via the Jobs tab"
        )
    try:
        if enabled:
            if job.is_paused:
                jobs_config.resume_job(jobs_cfg, _CHIEF_JOB_ID)
            job = jobs_config.update_job(
                jobs_cfg, _CHIEF_JOB_ID,
                schedule={"type": "daily", "at": at},
            )
        else:
            job = jobs_config.pause_job(jobs_cfg, _CHIEF_JOB_ID)
        if job is not None:
            jobs_mod.sync_schtasks(job)
    except ValueError as exc:
        return f"could not resync {_CHIEF_JOB_ID}: {exc}"
    return ""


@router.get("/api/board/chief/settings")
async def get_chief_settings(request: Request) -> Dict[str, Any]:
    """The chief settings block. Also the /chief skill's rails source: the
    worker cap is read from here over loopback, so it stays phone-tunable
    without a fleet-config commit."""
    cfg: WebappConfig = request.app.state.webapp_config
    return {"settings": _chief_settings_payload(cfg)}


@router.put("/api/board/chief/settings")
async def put_chief_settings(request: Request) -> Dict[str, Any]:
    """Persist chief settings; a respawn-time/enabled change resyncs the
    registered daily job (best-effort — an unregistered job is a warning in
    the response, never a failed save)."""
    cfg: WebappConfig = request.app.state.webapp_config
    body = await maybe_json(request)
    patch: Dict[str, Any] = {}
    if "model" in body:
        patch["chief_model"] = str(body.get("model") or "").strip().lower()
    if "respawn_enabled" in body:
        patch["chief_respawn_enabled"] = bool(body.get("respawn_enabled"))
    if "respawn_at" in body:
        patch["chief_respawn_at"] = str(body.get("respawn_at") or "").strip()
    if "worker_cap" in body:
        try:
            patch["chief_worker_cap"] = int(body.get("worker_cap"))
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400, detail="worker_cap must be an integer"
            )
    if not patch:
        raise HTTPException(status_code=400, detail="no chief settings in body")
    respawn_changed = (
        patch.get("chief_respawn_at", cfg.chief_respawn_at)
        != cfg.chief_respawn_at
        or patch.get("chief_respawn_enabled", cfg.chief_respawn_enabled)
        != cfg.chief_respawn_enabled
    )
    try:
        new_cfg = await asyncio.to_thread(update_webapp_config, **patch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    request.app.state.webapp_config = new_cfg
    job_warning = ""
    if respawn_changed:
        job_warning = await asyncio.to_thread(
            _sync_chief_respawn_job,
            new_cfg.chief_respawn_enabled,
            new_cfg.chief_respawn_at,
        )
        if job_warning:
            logger.warning("⚠️ chief settings: %s", job_warning)
    return {
        "settings": _chief_settings_payload(new_cfg),
        "job_warning": job_warning,
    }


async def _stop_chief_for_respawn(port: int, sid: str) -> None:
    """Gracefully quit the old chief, escalating to kill after the bounded
    wait — a fresh respawn must never end up with two chiefs."""
    try:
        await asyncio.to_thread(session_client.stop, port, sid, "quit")
    except session_client.SessionHostError as exc:
        logger.debug(f"chief respawn: quit failed ({exc}); will kill")
    deadline = time.monotonic() + CHIEF_STOP_WAIT_S
    while time.monotonic() < deadline:
        try:
            info = await asyncio.to_thread(session_client.get_session, port, sid)
        except session_client.SessionHostError:
            return  # gone — the host dropped it
        if not info.get("alive"):
            return
        await asyncio.sleep(CHIEF_STOP_POLL_S)
    try:
        await asyncio.to_thread(session_client.stop, port, sid, "kill")
    except session_client.SessionHostError:
        pass


@router.post("/api/board/chief/ensure")
async def ensure_chief(request: Request) -> Dict[str, Any]:
    """Spawn the fleet chief if none is alive (Tailscale + passkey, #245).

    Body/query: ``fresh`` truthy → kill the current chief first and respawn
    (the daily job's context-hygiene mode; the query form keeps the job a
    bodyless ``curl -X POST``). ``rows``/``cols`` size the PTY like every
    other launch. Returns ``{"session_id", "spawned"}`` — ``spawned`` False
    when an alive chief was found and kept.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    body = await maybe_json(request)
    fresh_raw = body.get("fresh", request.query_params.get("fresh"))
    fresh = str(fresh_raw).strip().lower() in ("1", "true", "yes")
    rows = int(body.get("rows") or 40)
    cols = int(body.get("cols") or 120)

    async with _CHIEF_ENSURE_LOCK:
        live = await asyncio.to_thread(
            _safe_list_sessions, cfg.session_host_port
        )
        chief = _find_chief(live)
        if chief:
            sid = str(chief.get("session_id") or "")
            if not fresh:
                return {"session_id": sid, "spawned": False}
            await _stop_chief_for_respawn(cfg.session_host_port, sid)

        entry = _resolve_repo_entry(cfg, _CHIEF_REPO)
        agent, flags = _agent_and_flags(cfg, cfg.chief_model)
        try:
            session = await asyncio.to_thread(
                spawn_claude_session,
                Path(entry.project_dir),
                _CHIEF_LABEL,
                flags,
                cfg.session_host_port,
                "pty",
                agent,
                rows,
                cols,
                history_lines=cfg.terminal_history_lines,
                label=_CHIEF_LABEL,
            )
        except session_client.SessionHostError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc))
        except OSError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        sid = str(session.get("session_id") or "")
        # Order matters (#245 review): rename FIRST, then /chief. The rename
        # also forwards the agent-native /rename into the PTY, so it must
        # land after boot but before the skill invocation — typed the other
        # way round it interleaves with /chief's processing and the agent
        # rejects it ("Args from unknown skill: rename"). The ready-wait
        # here plus _type_into_session's own settle give the rename a clear
        # beat before /chief goes in. Best-effort — a rename failure never
        # fails the spawn.
        try:
            await _await_dispatch_ready(cfg.session_host_port, sid)
        except HTTPException:
            try:
                await asyncio.to_thread(
                    session_client.stop, cfg.session_host_port, sid, "kill"
                )
            except session_client.SessionHostError:
                pass
            raise
        # First paint is not "input live" — wait for boot output to go
        # quiet before typing the rename, or its CR gets swallowed and the
        # text merges with the /chief paste (see _await_pty_quiescent). The
        # later _type_into_session call for the /chief command itself also
        # runs this wait, but by then boot has already settled here.
        await _await_pty_quiescent(cfg.session_host_port, sid)
        try:
            await asyncio.to_thread(
                session_client.rename,
                cfg.session_host_port, sid, _CHIEF_TITLE,
            )
        except session_client.SessionHostError as exc:
            logger.warning(
                "⚠️ chief ensure could not name session %s: %s",
                sid[:8], exc,
            )
        await _type_into_session(cfg.session_host_port, sid, _CHIEF_COMMAND)

        await audit_session_start_and_maybe_mirror(
            cfg, request, body,
            sid=sid, agent=agent, name=_CHIEF_LABEL,
            project=entry.project_dir, skill=_CHIEF_COMMAND,
            audit_mod=audit, mirror_fn=open_local_terminal_window,
        )
        return {"session_id": sid, "spawned": True, "session": session}
