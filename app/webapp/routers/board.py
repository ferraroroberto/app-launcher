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
                                            (?fresh=1 kills + restarts, an
                                            explicit operator action, #616;
                                            ?resume=1 reattaches the most
                                            recent chief conversation instead
                                            of starting fresh, #633) —
                                            Tailscale + passkey
    GET  /api/board/chief/settings        → chief settings block (also read
                                            by the /chief skill over loopback)
    PUT  /api/board/chief/settings        → persist chief settings

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
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request

from src import agents, audit, board, github_client, session_client
from src.board_exchange import resolve_exchange, unavailable
from src.launcher import open_local_terminal_window, spawn_claude_session
from src.registry import AppEntry, live_claude_code_entries
from src.webapp_config import (
    WebappConfig,
    build_claude_flags,
    build_codex_flags,
    build_resume_flags,
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

    live = _reconcile_chief_labels(live, state["rows"])
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
    native_name_flags = agents.native_session_name_flags_for(agent, title)
    flags = " ".join(
        part for part in (base_flags, native_name_flags, f'"{prompt}"') if part
    )
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

    Framing, the CR-as-a-separate-write ordering (#64/#166), and — for a
    long ``command`` — the settle-then-submit wait (#611) all now live
    session-host-side in ``PtySession.submit_input``, keeping the text one
    atomic paste with no per-keystroke TUI interpretation and routing it
    through the first-prompt title capture (#266). Framing is applied only
    when the PTY's own DECSET 2004 output says bracketed-paste mode is on —
    always true by the time this fires, since typing happens after both the
    readiness and quiescence waits below, well past the agent's own paste-
    mode announcement during boot. The quiescence wait (#549) guards against
    typing while the agent's boot output is still growing, which can swallow
    the submitting CR — first-paint alone is not enough. On any failure past
    the spawn the half-spawned session is killed, so a timeout can't strand
    an orphan the user never asked for. Shared by dispatch (#302) and the
    chief ensure (#245) so the timing rules stay single-sourced instead of
    drifting between call sites.
    """
    try:
        await _await_dispatch_ready(port, sid)
        await _await_pty_quiescent(port, sid)
        await asyncio.to_thread(
            session_client.send_input, port, sid, command, True,
        )
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
# in fleet-config. Two triggers hit the same ensure endpoint: the first
# chat-mode message (lazy) and the manual Start/Restart button (#617). A
# third trigger — an unattended daily respawn job — was retired in #616:
# fleet-config#442/#449 shipped compact-and-continue (chief hands its own
# handover log back to itself on every session start), so a schedule that
# force-restarted chief unattended would now discard a live batch's context
# instead of protecting it. `fresh=1` (a graceful stop-then-respawn) is kept
# as an explicit operator action only, still used by the manual Restart
# button — nothing calls it unattended anymore.

_CHIEF_REPO = "fleet-config"
_CHIEF_LABEL = "chief"
_CHIEF_COMMAND = "/chief"
_CHIEF_TITLE = "chief"
# How long a fresh respawn waits for the old chief's graceful stop before
# escalating to kill. Module-level so tests can patch it tiny.
CHIEF_STOP_WAIT_S = 8.0
CHIEF_STOP_POLL_S = 0.25

# Two concurrent ensures (e.g. a chat send racing a manual Restart) must not
# double-spawn; the lock serializes the check-then-spawn window.
_CHIEF_ENSURE_LOCK = asyncio.Lock()

# The directory-name grain every "is this fleet-config?" check in this repo
# already uses (board_state.py's project fallback, the state-row `project`
# field) — cheap and consistent, no registry scan needed.
_CHIEF_PROJECT_DIR_NAME = "fleet-config"


def _live_title_names_chief(sess: Dict[str, Any]) -> bool:
    """Whether ``live_title`` — the OSC window-title session_host parses
    directly off the PTY's own raw output (``PtySession._read_loop``) —
    names this conversation "chief" (#628).

    This is the fastest of the three self-heal signals: it needs no hook and
    no transcript read, just Claude Code's own title escape sequence, which
    docs/board.md already documents as updating "sub-second inside an open
    terminal, ahead of the next state-file poll". Verified live against the
    real resumed chief session (7174c1d2…, the one this issue's own
    "Constraints worth knowing" section cites): at a moment when
    ``prompt_title`` and the hook-state ``shared_name`` had not yet
    identified it, ``live_title`` already read the conversation's
    self-declared title.

    Matched on the *last whitespace-separated token*, not equality — Claude
    prefixed its own title with an emoji (observed: a crown) in that live
    case, which carries no fixed spelling to match against, so pinning down
    only the trailing word avoids depending on which emoji (if any) it
    chooses.
    """
    title = str(sess.get("live_title") or "").strip()
    if not title:
        return False
    return title.split()[-1].strip().lower() == _CHIEF_TITLE


def _reconcile_chief_label(sess: Dict[str, Any], shared_name: Any) -> Dict[str, Any]:
    """Self-heal ``label`` for a chief PTY spawned outside ``ensure`` (#617).

    ``ensure_chief`` is spawn-if-absent, but it is not the only way a chief
    gets started. Two ways it slips past the label: Roberto opens a plain
    Coding-tab session in fleet-config and types ``/chief`` himself, or — the
    case actually observed live, verified against the real session
    (1c8e6dde…) rather than only synthetic tests — the session-host restarts,
    killing the PTY, and he re-attaches the *same underlying Claude Code
    conversation* via Resume. Either way the launcher never gets to pass
    ``label="chief"`` at spawn, so every consumer keying on it — this
    router's own ``_find_chief``, ``chief_ops.py``'s worker-cap count and
    ``chief-sid`` lookup (both read straight off ``GET /api/board``, so this
    reconciliation is the only place any of them needs fixing), the Board's
    crown/tint — silently treats a live, working chief as not running.

    Three independent, narrow signals, any one sufficient on its own — not a
    stack of guesses, three genuinely different things a chief session does,
    each read from wherever Claude Code's own self-declared identity surfaces
    earliest:

    * ``live_title`` (#628, :func:`_live_title_names_chief`): checked first
      here because it is available earliest — Claude Code re-emits its own
      established OSC title on a Resume, before any hook fires or the user
      types anything, closing the exact gap #628 was filed over (a resumed
      chief unidentifiable "until its first hook fires").
    * ``prompt_title`` (#266): the session-host's own capture of the first
      line ever *submitted* into this PTY. Exact for a freshly typed
      ``/chief`` — but a Resume never re-submits it (the conversation is
      already past that point), so this alone misses exactly the observed
      case.
    * ``shared_name`` (fleet-config#302): Claude Code's own self-derived name
      for the *conversation*, not the PTY — read from its live per-process
      registry via the hook state file, joined by the same agent-aware claim
      walk every other cross-tab title uses (:func:`board.attach_shared_names`).
      This persists across a Resume into a brand-new PTY, because it belongs
      to the conversation, not the process that's currently attached to it —
      but it needs a hook to have fired at least once, which is exactly the
      window ``live_title`` above closes.

    All three are scoped to a live PTY cwd'd in the fleet-config checkout — a
    directory name alone proves nothing (the dead ``name == "chief"``
    fallback below learned that the hard way: it reads the *launcher's*
    session name, which is the project name ("fleet-config") for a
    Resume-launched session, never "chief").

    Read-only: never mutates the session-host's own record, only the dict
    this process just fetched from it.
    """
    if sess.get("label") or sess.get("kind") != "pty":
        return sess
    if Path(str(sess.get("project_dir") or "")).name != _CHIEF_PROJECT_DIR_NAME:
        return sess
    prompt_title = str(sess.get("prompt_title") or "").strip()
    shared_name_norm = str(shared_name or "").strip().lower()
    if (
        prompt_title != _CHIEF_COMMAND
        and shared_name_norm != _CHIEF_TITLE
        and not _live_title_names_chief(sess)
    ):
        return sess
    logger.info(
        "👑 chief label self-healed for session %s (spawned outside ensure)",
        str(sess.get("session_id") or "")[:8],
    )
    return {**sess, "label": _CHIEF_LABEL}


def _reconcile_chief_labels(
    live: List[Dict[str, Any]], state_rows: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Apply :func:`_reconcile_chief_label` across a live session list.

    Needs both the live list and the hook state rows together (``shared_name``
    only exists after the state-row join), so this can't live inside
    ``_safe_list_sessions`` (``port``-only) — callers that need a
    chief-reconciled live list fetch both and call this, or go through
    :func:`_live_sessions_with_chief_label`.
    """
    named = board.attach_shared_names(live, state_rows)
    shared_names = {
        str(item.get("session_id")): item.get("shared_name") for item in named
    }
    return [
        _reconcile_chief_label(sess, shared_names.get(str(sess.get("session_id"))))
        for sess in live
    ]


async def _live_sessions_with_chief_label(
    cfg: WebappConfig,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Live sessions + hook state, fetched together and chief-reconciled (#617).

    Returns ``(live, state)`` so a caller that also needs ``state["rows"]``
    for its own purposes (``get_board`` builds cards from it) doesn't fetch
    the state file twice.
    """
    live, state = await asyncio.gather(
        asyncio.to_thread(_safe_list_sessions, cfg.session_host_port),
        asyncio.to_thread(board.read_sessions_state, Path(cfg.sessions_state_file)),
    )
    return _reconcile_chief_labels(live, state["rows"]), state


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


def _find_resumable_chief_session_id(
    state_rows: Dict[str, Any], *, now: Optional[datetime] = None
) -> str:
    """The most recent fleet-config chief conversation id, or ``""`` (#633).

    Filters ``sessions-state.json`` rows to the ones that are both in the
    fleet-config checkout (``project``, falling back to ``Path(cwd).name`` —
    the same fallback ``board_sessions._external_row`` already applies) and
    named ``"chief"`` (case-insensitive, mirroring ``_CHIEF_TITLE``), then
    returns the dict key — Claude's own session UUID, exactly what
    ``claude --resume <id>`` needs — of the newest by ``updated_at``.

    Applies ``board.STATE_STALE_AFTER`` (24h) per row rather than trusting
    the file-level ``stale`` flag some *other* row could keep looking fresh:
    a chief conversation that's individually gone cold must not be resumed
    just because a different session kept the file's newest timestamp
    recent. Returns ``""`` (never raises) when nothing qualifies — same
    degradation contract every other ``board_state`` reader already follows;
    the caller treats that as "fall back to a fresh spawn", not an error.
    """
    now = now or board._now()
    best_sid = ""
    best_stamp = None
    for sid, row in state_rows.items():
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip().lower()
        if name != _CHIEF_TITLE:
            continue
        cwd = row.get("cwd")
        project = str(row.get("project") or Path(str(cwd or "")).name)
        if project != _CHIEF_PROJECT_DIR_NAME:
            continue
        stamp = board._parse_iso(row.get("updated_at"))
        if stamp is None or now - stamp > board.STATE_STALE_AFTER:
            continue
        if best_stamp is None or stamp > best_stamp:
            best_stamp = stamp
            best_sid = str(sid)
    return best_sid


def _chief_settings_payload(cfg: WebappConfig) -> Dict[str, Any]:
    return {
        "model": cfg.chief_model,
        "worker_cap": cfg.chief_worker_cap,
    }


@router.get("/api/board/chief/settings")
async def get_chief_settings(request: Request) -> Dict[str, Any]:
    """The chief settings block. Also the /chief skill's rails source: the
    worker cap is read from here over loopback, so it stays phone-tunable
    without a fleet-config commit."""
    cfg: WebappConfig = request.app.state.webapp_config
    return {"settings": _chief_settings_payload(cfg)}


@router.put("/api/board/chief/settings")
async def put_chief_settings(request: Request) -> Dict[str, Any]:
    """Persist chief settings."""
    body = await maybe_json(request)
    patch: Dict[str, Any] = {}
    if "model" in body:
        patch["chief_model"] = str(body.get("model") or "").strip().lower()
    if "worker_cap" in body:
        try:
            patch["chief_worker_cap"] = int(body.get("worker_cap"))
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400, detail="worker_cap must be an integer"
            )
    if not patch:
        raise HTTPException(status_code=400, detail="no chief settings in body")
    try:
        new_cfg = await asyncio.to_thread(update_webapp_config, **patch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    request.app.state.webapp_config = new_cfg
    return {"settings": _chief_settings_payload(new_cfg)}


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
    — the manual Restart button's mode (#616/#617; the query form keeps a
    bodyless ``curl -X POST`` usable for a manual operator restart). ``resume``
    truthy → reattach the most recent chief conversation instead of starting
    fresh (#633): stops any live chief first (same as ``fresh`` — never end up
    with two), looks up the newest same-day ``name == "chief"`` row in
    ``sessions-state.json`` for the fleet-config checkout
    (:func:`_find_resumable_chief_session_id`), and — if found — spawns with
    ``label="chief"`` declared at spawn time and a direct
    ``claude --resume <id>`` (never the bare interactive picker), skipping the
    ``/chief`` type-in entirely since a resumed conversation is already past
    that point. A chief stopped for a ``resume`` request is thus resumed right
    back into itself — a context-preserving restart, not a coincidence of the
    shared stop-first ordering with ``fresh``. No resumable id (state pruned
    past the 24h window, or no prior chief) degrades to today's fresh-spawn-
    and-``/chief`` path, never a hard failure — the response's ``resumed`` /
    ``resume_fallback_reason`` fields tell the caller which happened.
    ``rows``/``cols`` size the PTY like every other launch. Returns
    ``{"session_id", "spawned", "resumed", "resume_fallback_reason"}`` —
    ``spawned`` False when an alive chief was found and kept (only possible
    when neither ``fresh`` nor ``resume`` was requested).
    """
    cfg: WebappConfig = request.app.state.webapp_config
    body = await maybe_json(request)
    fresh_raw = body.get("fresh", request.query_params.get("fresh"))
    fresh = str(fresh_raw).strip().lower() in ("1", "true", "yes")
    resume_raw = body.get("resume", request.query_params.get("resume"))
    resume = str(resume_raw).strip().lower() in ("1", "true", "yes")
    rows = int(body.get("rows") or 40)
    cols = int(body.get("cols") or 120)

    async with _CHIEF_ENSURE_LOCK:
        live, state = await _live_sessions_with_chief_label(cfg)
        chief = _find_chief(live)
        if chief:
            sid = str(chief.get("session_id") or "")
            if not fresh and not resume:
                return {"session_id": sid, "spawned": False}
            await _stop_chief_for_respawn(cfg.session_host_port, sid)

        resumed_session_id = (
            _find_resumable_chief_session_id(state["rows"]) if resume else ""
        )
        resume_fallback_reason = (
            "no resumable chief conversation found in the last 24h"
            if resume and not resumed_session_id
            else ""
        )

        entry = _resolve_repo_entry(cfg, _CHIEF_REPO)
        if resumed_session_id:
            agent = "claude"
            flags = build_resume_flags(
                cfg, agent, model_override=cfg.chief_model,
                session_id=resumed_session_id,
            )
        else:
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
        # fails the spawn. A resumed conversation (#633) skips /chief below —
        # it's already past that point — but still gets the same rename so
        # its title reads "chief" immediately rather than waiting on the
        # self-heal signals in _reconcile_chief_label.
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
        if not resumed_session_id:
            await _type_into_session(cfg.session_host_port, sid, _CHIEF_COMMAND)

        await audit_session_start_and_maybe_mirror(
            cfg, request, body,
            sid=sid, agent=agent, name=_CHIEF_LABEL,
            project=entry.project_dir,
            skill=None if resumed_session_id else _CHIEF_COMMAND,
            resume=bool(resumed_session_id),
            audit_mod=audit, mirror_fn=open_local_terminal_window,
        )
        return {
            "session_id": sid,
            "spawned": True,
            "session": session,
            "resumed": bool(resumed_session_id),
            "resume_fallback_reason": resume_fallback_reason,
        }
