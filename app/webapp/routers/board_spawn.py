"""Shared spawn-then-type mechanics for the Board tab's launch paths.

Split off ``app/webapp/routers/board.py`` (issue #691, a `/codebase-audit`
maintainability finding), the same way ``jobs_run.py`` was split off
``jobs.py``. This module owns the parts of the Board's launch path that are
reused across *both* route modules:

* :func:`_read_live_sessions` — the degradation-safe live-session read,
  carrying its unknown state (#915) so no caller reads a failed list as empty.
* :func:`_resolve_repo_entry` — bare repo name → live projects-folder entry.
* :func:`_agent_and_flags` — the Board's per-launch model selector (#500/#505).
* :func:`_dispatch_effort` / :func:`_applied_effort` — a dispatch's per-launch
  ``effort``: checked against the model, then read back off the built flags.
* :func:`_await_dispatch_ready` / :func:`_await_pty_quiescent` /
  :func:`_type_into_session` — the readiness, quiescence and framing rules for
  writing text into a freshly spawned PTY (#64/#166/#245/#302/#549/#611).

Kept as its own module so neither route module has to import the other:
``board.py`` (columns, drill-down, issue-start) and
``board_chief.py`` (the fleet-chief lifecycle) both need this machinery, and
``board.py`` already imports ``board_chief`` to mount its router — a shared
dependency, not a ``board.py`` <-> ``board_chief.py`` cycle.

The timing constants are module-level so tests can patch them tiny; patch them
**here**, not on the route modules that call through.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from src import agents
from src.clients import session as session_client
from src.launching.flags import build_claude_flags, build_codex_flags, build_grok_flags
from src.launching.model_catalog import (
    CLAUDE_MODEL_SPECS, CODEX_MODEL_SPECS, GROK_DISPATCH_MODEL_SPECS, available_values,
)
from src.registry import AppEntry, live_claude_code_entries
from src.webapp_config import VALID_CODEX_MODELS, VALID_GROK_EFFORTS, WebappConfig

logger = logging.getLogger(__name__)


# The reason a per-session route reports when the session-host's list could
# not be read: distinct from ``session_not_found``, which means the
# list *was* read and the session is not in it. Clients treat the first as
# transient (keep polling, backed off) and the second as terminal.
SESSION_HOST_UNREACHABLE = "session_host_unreachable"

# Latched so a session-host that stays down leaves one warning, not one per
# 5 s Board poll; the recovery gets its own breadcrumb.
_session_host_down = False


def _read_live_sessions(port: int) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """``(sessions, error)`` — ``error`` set means the list is *unknown*.

    The Board must keep rendering GitHub + jobs cards when the session-host is
    down (#164 degradation), but an empty list it could not read is not an
    empty list it read (#915): ``GET /api/board`` carries ``error`` through so
    the render never says "Nothing needs you right now" on a failure.
    """
    global _session_host_down
    try:
        sessions = session_client.list_sessions(port)
    except session_client.SessionHostError as exc:
        if _session_host_down:
            logger.debug("board: session list still failing: %s", exc)
        else:
            logger.warning(
                "⚠️ board: session-host list failed, live sessions unknown: %s", exc
            )
        _session_host_down = True
        return [], str(exc)
    if _session_host_down:
        logger.info("✅ board: session-host list readable again")
        _session_host_down = False
    return sessions, None


def _resolve_repo_entry(cfg: WebappConfig, repo: str) -> AppEntry:
    """Resolve ``repo`` to a live claude-code entry, or 404.

    Shared by ``start_issue`` and the chief ensure — both
    take a bare repo name (or the hardcoded ``fleet-config``) and need the
    same case-insensitive lookup against the live projects-folder listing.
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


# Dispatch readiness: how long to wait for the freshly spawned agent
# to paint its first output before typing into it, the settle after that
# first paint, and the fixed grace for a session-host old enough to not
# report ``output_chars`` yet. Module-level so tests can patch them tiny.
DISPATCH_READY_CAP_S = 15.0
DISPATCH_SETTLE_S = 2.0
DISPATCH_POLL_S = 0.25
DISPATCH_LEGACY_GRACE_S = 5.0

# PTY input-quiescence: first-paint + a fixed
# settle is NOT always "input ready" for a fresh --remote-control agent — a
# CR typed while boot output (handshake, banner) is still growing gets
# swallowed, leaving the typed text sitting unsubmitted (e.g. a
# chief-dispatched /issue-add worker left idle with its goal typed but
# never submitted). Stable output for PTY_QUIESCENT_STABLE_S is the
# strongest cheap signal the prompt has settled; cap and proceed
# best-effort rather than failing the spawn.
# Module-level so tests can patch them tiny.
PTY_QUIESCENT_STABLE_S = 2.0
PTY_QUIESCENT_CAP_S = 30.0
PTY_QUIESCENT_POLL_S = 0.5

# Provider-qualified choices come from the shared model catalog.
_DISPATCH_CLAUDE_MODELS = available_values(CLAUDE_MODEL_SPECS)

# The id only gates the provider-qualified value; flags carry the Coding tab's
# effort and permission mode.
_DISPATCH_GROK_MODELS = available_values(GROK_DISPATCH_MODEL_SPECS)


def _agent_and_flags(
    cfg: WebappConfig, model: str, effort: Optional[str] = None
) -> Tuple[str, str]:
    """Validated ``(agent, flags)`` for a Board per-launch ``model`` (#500/#505).

    The Claude tiers force ``--model`` like the Life OS tab's toggle (#102);
    Codex values select an exact model with the Coding tab's remembered
    compatible effort; ``grok:grok-4.5`` takes the Coding tab's Grok effort and
    permission mode. The ``is_installed`` check is the
    same defence-in-depth 400 as ``apps.py`` — Board launches bypass the
    Coding tab's already-disabled button.

    ``effort`` (already checked by :func:`_dispatch_effort`) replaces the
    persisted effort for this one launch; ``None`` keeps it.
    """
    provider, model_value = _split_model(model)
    if provider == "claude" and model_value in _DISPATCH_CLAUDE_MODELS:
        return "claude", build_claude_flags(cfg, model_value, effort_override=effort)
    if provider == "grok" and model_value in _DISPATCH_GROK_MODELS:
        _require_installed("grok")
        return "grok", build_grok_flags(cfg, effort_override=effort)
    if provider != "codex" or model_value not in VALID_CODEX_MODELS:
        raise HTTPException(status_code=400, detail=f"unknown model: {model}")
    _require_installed("codex")
    return "codex", build_codex_flags(cfg, model_value, effort_override=effort)


def _split_model(model: str) -> Tuple[str, str]:
    """``(provider, model)``; a bare value is a Claude tier, as old callers send."""
    provider, separator, model_value = model.partition(":")
    return (provider, model_value) if separator else ("claude", model)


def _efforts_for(model: str) -> Tuple[str, ...]:
    """The per-launch efforts ``model`` offers, ``()`` when it takes none."""
    provider, model_value = _split_model(model)
    if provider == "claude":
        return tuple(CLAUDE_MODEL_SPECS.get(model_value, {}).get("efforts", ()))
    if provider == "codex":
        return tuple(CODEX_MODEL_SPECS.get(model_value, {}).get("efforts", ()))
    return tuple(VALID_GROK_EFFORTS) if provider == "grok" else ()


def _dispatch_effort(
    effort: Optional[str], requested: str, launched: str
) -> Optional[str]:
    """The effort to put on a dispatch's command line, ``None`` for the persisted one.

    A value the ``requested`` model does not offer is a 400 naming the allowed
    ones. When the Grok auto-switch ``launched`` a different model, a value
    that model does not offer is dropped rather than failing a dispatch that
    was valid as sent; :func:`_applied_effort` then echoes nothing.
    """
    if effort is None:
        return None
    allowed = _efforts_for(requested)
    if effort not in allowed:
        offered = ", ".join(allowed) if allowed else "none"
        raise HTTPException(
            status_code=400,
            detail=f"effort {effort!r} is not offered by {requested}; allowed: {offered}",
        )
    return effort if effort in _efforts_for(launched) else None


# The flag each agent carries its effort in, as built by src.launching.flags.
_EFFORT_FLAG = {"claude": "--effort", "grok": "--reasoning-effort"}


def _applied_effort(agent: str, flags: str, effort: Optional[str]) -> Optional[str]:
    """``effort`` when the built ``flags`` actually carry it, else ``None``.

    Read off the command line, never assumed from the request, so the
    dispatcher can report the effort as applied from evidence.
    """
    if effort is None:
        return None
    parts = flags.split()
    if agent == "codex":
        return effort if f"model_reasoning_effort={effort}" in parts else None
    flag = _EFFORT_FLAG.get(agent)
    pairs = zip(parts, parts[1:])
    return effort if flag and (flag, effort) in pairs else None


def _require_installed(agent_id: str) -> None:
    """400 naming the agent when its CLI is not on this host."""
    if not agents.is_installed(agent_id):
        raise HTTPException(
            status_code=400,
            detail=f"{agents.AGENTS[agent_id].label} is not installed",
        )


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
    an orphan the user never asked for. Shared by the chief ensure (#245) and
    its siblings so the timing rules stay single-sourced instead of
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
