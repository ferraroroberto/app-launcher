"""Board tab — the fleet kanban's data plane (issues #300, #301, #302 / #164 / #399).

    GET  /api/board                       → the five computed columns (token-gated)
    GET  /api/board/chief-plan            → the chief's plan + answered marks (token-gated)
    POST /api/board/chief/answered        → record answered questions (#1487;
                                            Tailscale + passkey)
    POST /api/board/github/refresh        → run the gh searches now (token-gated)
    GET  /api/board/sessions/{sid}/exchange → last user↔assistant exchange
                                            (Tailscale + passkey — transcript text)
    POST /api/board/issues/start          → spawn /issue-start|yolo <N> in the
                                            issue's repo (Tailscale + passkey)

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
``cmd /c`` line can never contain a metacharacter. An optional dispatch brief
(#1114) keeps that property: its text goes to a launcher-owned file and only
the uuid-named path rides the prompt as ``--brief <path>``.
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
    chief_answers,
    chief_plan,
    dispatch_brief,
    github_client,
    jobs_snapshot,
    quota_usage,
    session_client,
)
from src.board_exchange import resolve_exchange, unavailable
from src.launch_flags import build_claude_flags
from src.launcher import open_local_terminal_window, spawn_claude_session
from src.webapp_config import WebappConfig

from app.webapp.routers import board_chief
from app.webapp.routers._helpers import (
    attach_provider_web_urls,
    audit_session_start_and_maybe_mirror,
    maybe_json,
    safe_int,
    spawn_launcher_session,
)
from app.webapp.routers.board_spawn import (
    SESSION_HOST_UNREACHABLE,
    _agent_and_flags,
    _read_live_sessions,
    _resolve_repo_entry,
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
        Path(cfg.fleet_config_dir),
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
                Path(cfg.fleet_config_dir),
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
    # No quota here (#1433): both tabs' usage meters read ``/api/rate-limits``,
    # one endpoint, so the two can never show different readings.
    live_read, state, active_issues, job_cards = await asyncio.gather(
        asyncio.to_thread(_read_live_sessions, cfg.session_host_port),
        asyncio.to_thread(board.read_sessions_state, Path(cfg.sessions_state_file)),
        asyncio.to_thread(board.read_active_issues, active_issues_file),
        asyncio.to_thread(board.jobs_attention),
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
        fleet_config_dir=Path(cfg.fleet_config_dir),
    )
    # Per-card transcript reads — unbounded in session count and re-run every
    # 5s while the Board is open, so it goes off the loop like the five
    # inputs above (#881).
    session_cards = await asyncio.to_thread(
        board.merge_sessions,
        live, state["rows"],
        active_issue_repos=board.active_issue_repos(active_issues["rows"], claim_states),
    )
    # The drawer's Rename opens the same Rename / link dialog as the Coding
    # tab, which reads ``web_url`` — without this it said "Not available yet"
    # for a live Claude PTY session the Coding tab linked fine (#1096). Same
    # bounded transcript scan, off the loop and memoized in _helpers, so the
    # Board poll re-uses whatever the (identically-paced) sessions poll found.
    await asyncio.to_thread(attach_provider_web_urls, session_cards)
    columns = board.build_board(session_cards, github, job_cards)
    _mark_active_backlog(columns, active_issues["rows"], claim_states)

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
        # Age of the jobs runtime snapshot the job cards came from (#1324),
        # so a stale one can never pass for current.
        "jobs_snapshot": jobs_snapshot.latest_description(),
    }


@router.get("/api/board/chief-plan")
async def get_chief_plan(request: Request) -> Dict[str, Any]:
    """The chief's plan for the Board's "Chief's plan" card (#1279).

    Read-only, polled alongside ``/api/board``: ``state`` is ``ok`` (with
    ``updated_at``, ``lanes``, ``queue``, ``waiting_on_roberto``), ``empty``
    or ``unreadable``. Whether the chief is running is the client's to say,
    from the session cards ``/api/board`` already carries.

    An ``ok`` plan also carries ``answered`` (#1487): the keys of the
    questions any device already answered on this version of the plan, so
    every device marks them alike.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    return await asyncio.to_thread(_read_plan_with_answers, cfg)


def _read_plan_with_answers(cfg: WebappConfig) -> Dict[str, Any]:
    plan = chief_plan.read_chief_plan(Path(cfg.chief_plan_file), cfg.github_owner)
    if plan["state"] == "ok":
        plan["answered"] = chief_answers.read_answered(plan["updated_at"])
    return plan


@router.post("/api/board/chief/answered")
async def post_chief_answered(request: Request) -> Dict[str, Any]:
    """Record the questions the answer sheet just sent (#1487; Tailscale +
    passkey, as every ``/api/board/chief/`` route).

    Body: ``{"updated_at": <the plan version answered>, "ids": [<key>]}``.
    Recorded only while that is still the plan on disk; a plan the chief has
    since rewritten has no use for the marks, so ``recorded`` is ``false``
    and nothing is written. Either way the reply carries the current plan's
    ``updated_at`` and ``answered`` keys. Never touches the plan file.
    """
    body = await maybe_json(request)
    updated_at = body.get("updated_at")
    ids = chief_answers.clean_ids(body.get("ids"))
    if not isinstance(updated_at, str) or ids is None:
        raise HTTPException(
            status_code=400, detail="updated_at must be a string and ids a list of strings"
        )
    cfg: WebappConfig = request.app.state.webapp_config
    plan = await asyncio.to_thread(_read_plan_with_answers, cfg)
    if plan["state"] != "ok":
        return {"recorded": False, "updated_at": "", "answered": []}
    if plan["updated_at"] != updated_at:
        return {"recorded": False, "updated_at": plan["updated_at"], "answered": plan["answered"]}
    try:
        answered = await asyncio.to_thread(chief_answers.record_answered, updated_at, ids)
    except OSError as exc:
        logger.warning("⚠️ chief answered marks not written: %s", exc)
        raise HTTPException(status_code=500, detail="answered marks not written")
    return {"recorded": True, "updated_at": updated_at, "answered": answered}


@router.get("/api/rate-limits")
async def get_rate_limits(request: Request) -> Dict[str, Any]:
    """The quota rows behind both tabs' usage meters (#1433).

    The one quota endpoint: the Coding tab's full meter and the Board's
    compact one both render from the client's single poll of this route,
    which runs whatever tab is up, so neither depends on the other having
    been opened and the two can never disagree. ``GET /api/board`` carries
    no quota of its own.

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

    A consulted scan also reports ``title_check`` (#1034): the PTY window
    title can *disprove* a scanned conversation — refusing it in favour of
    the capture when the two name different conversations — and says
    ``unknown`` when it cannot settle the question rather than passing for
    agreement. See ``transcript_locate._disprove_by_live_title``.
    """
    cfg: WebappConfig = request.app.state.webapp_config
    (live, host_error), state = await asyncio.gather(
        asyncio.to_thread(_read_live_sessions, cfg.session_host_port),
        asyncio.to_thread(board.read_sessions_state, Path(cfg.sessions_state_file)),
    )
    session = next(
        (item for item in live if str(item.get("session_id")) == str(sid)), None
    )
    if session is None:
        # An unreadable session list is not "the session ended" (#1308): the
        # session may be alive behind a session-host that timed out once.
        return unavailable(
            SESSION_HOST_UNREACHABLE if host_error else "session_not_found"
        )
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


def _opens_pc_window(chief_dispatch: bool, body: Dict[str, Any]) -> bool:
    """Whether a Board launch may open the PC mirror window (#1283).

    The fleet chief dispatches over loopback with no browser flag, and its
    workers run unwatched: a window per dispatch put its whole queue on
    Roberto's desktop. Such a launch opens none; the window comes when he
    opens the session himself (the Code tab's row, or the mirror action).
    A browser's own Board tap (``desktop`` or ``in_page`` set) keeps the
    usual rule, chief running or not.
    """
    return not chief_dispatch or bool(body.get("desktop")) or bool(body.get("in_page"))


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

    The optional ``brief`` (#1114, fleet-config#944) is a dispatcher's scope,
    queue and constraints for the lane, carried on the one channel the lane
    trusts — its launch command. The text is written to a launcher-owned file
    (:mod:`src.dispatch_brief`) and only that uuid-named path is appended as
    ``--brief <path>``, so the invariant above still holds. Absent (or
    ``null``) → the prompt is byte-identical to a brief-less start; empty or
    oversize → 400.
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
    brief = None
    if body.get("brief") is not None:
        try:
            brief = dispatch_brief.validate_brief(body.get("brief"))
        except dispatch_brief.BriefError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    if model:
        agent, base_flags = _agent_and_flags(cfg, model)
    else:
        agent, base_flags = "claude", build_claude_flags(cfg)

    entry = _resolve_repo_entry(cfg, repo)

    prompt = f"/issue-{mode} {number}"
    brief_path = None
    if brief is not None:
        brief_path = await asyncio.to_thread(dispatch_brief.write_brief, brief)
        prompt = f"{prompt} --brief {brief_path.as_posix()}"
    native_name_flags = agents.native_session_name_flags_for(agent, title)
    flags = " ".join(
        part for part in (base_flags, native_name_flags, f'"{prompt}"') if part
    )
    try:
        session, sid = await spawn_launcher_session(
            spawn_claude_session, cfg,
            project_dir=Path(entry.project_dir), name=entry.name,
            flags=flags, agent=agent, rows=rows, cols=cols,
        )
    except Exception:
        # No lane will ever read it — don't leave it for the TTL sweep.
        if brief_path is not None:
            await asyncio.to_thread(dispatch_brief.discard_brief, brief_path)
        raise
    chief_dispatch = await board_chief._mark_chief_managed(
        cfg, request, sid, entry.name, number)
    await audit_session_start_and_maybe_mirror(
        cfg, request, body,
        sid=sid, agent=agent, name=entry.name, project=entry.project_dir,
        skill=prompt, audit_mod=audit, mirror_fn=open_local_terminal_window,
        brief_chars=len(brief) if brief is not None else None,
        mirror=_opens_pc_window(chief_dispatch, body),
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
