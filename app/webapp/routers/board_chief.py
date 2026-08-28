"""Fleet-chief lifecycle — the standing orchestrator behind the Board's chat mode.

    POST /api/board/chief/ensure    → spawn the fleet chief if absent
                                      (?fresh=1 kills + restarts, an explicit
                                      operator action, #616; ?resume=1
                                      reattaches the most recent *substantive*
                                      chief conversation instead of starting
                                      fresh, #633/#670) — Tailscale + passkey
    GET  /api/board/chief/settings  → chief settings block (also read by the
                                      /chief skill over loopback)
    PUT  /api/board/chief/settings  → persist chief settings

Split off ``app/webapp/routers/board.py`` (issue #691, a `/codebase-audit`
maintainability finding) — the same god-router split ``jobs.py`` and
``sessions.py`` already carry. ``board.py`` owned two unrelated concerns: the
Board tab's read-only column assembly, and this ~650-line chief state machine
(spawn, resume, label self-heal, chief-managed marking, settings). Mounted on
``board.router`` via ``include_router`` so ``app/webapp/server.py`` still
registers one ``board.router``.

Imports flow one way: this module depends on
:mod:`app.webapp.routers.board_spawn` (the shared spawn-then-type mechanics)
and never on ``board.py``, which imports *this* — so there is no cycle. The
chief-label self-heal lives here rather than in ``board.py`` even though
``GET /api/board`` calls it: it is chief-domain logic, and the poll reaching
into this module is one call, not a shared concern.

The chief is a normal PTY session (label="chief") spawned in the fleet-config
checkout — that cwd is what loads the fleet-only /chief skill tier and keeps
app-launcher's own project context out of it. The server ships zero chief
prose: the spawn types only "/chief", so the brain stays versioned in
fleet-config. Two triggers hit the ensure endpoint: the first chat-mode message
(lazy) and the manual Start/Restart button (#617). A third trigger — an
unattended daily respawn job — was retired in #616: fleet-config#442/#449
shipped compact-and-continue (chief hands its own handover log back to itself
on every session start), so a schedule that force-restarted chief unattended
would now discard a live batch's context instead of protecting it. ``fresh=1``
(a graceful stop-then-respawn) is kept as an explicit operator action only,
still used by the manual Restart button — nothing calls it unattended anymore.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request

from src import audit, board, chief_pointer, session_client
from src.launch_flags import build_resume_flags
from src.launcher import open_local_terminal_window, spawn_claude_session
from src.subprocess_flags import NO_WINDOW
from src.webapp_config import WebappConfig, update_webapp_config

from app.webapp.routers._helpers import (
    audit_session_start_and_maybe_mirror,
    client_ip,
    maybe_json,
    safe_int,
    spawn_session_or_400,
)
from app.webapp.routers.board_spawn import (
    _agent_and_flags,
    _await_dispatch_ready,
    _await_pty_quiescent,
    _resolve_repo_entry,
    _safe_list_sessions,
    _type_into_session,
)

logger = logging.getLogger(__name__)
router = APIRouter()

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


async def _mark_chief_managed(
    cfg: WebappConfig, request: Request, sid: str, repo: str, number: int
) -> None:
    """Best-effort: record a launcher-dispatched worker as chief-managed
    (fleet-config#474) so `hooks/notify_on_idle.py` and
    `hooks/block_askuserquestion_chief.py` route it like one dispatched
    through `chief_ops.py dispatch` -- the CLI-only path that already
    marked correctly. `start_issue`/`dispatch_goal` are the paths chief
    actually calls (over loopback, never through the CLI wrapper), so
    without this the marker was never written for a real dispatch.

    Gated on two signals already meaningful in this codebase rather than a
    new protocol: the caller is loopback (the same trust `BearerTokenMiddleware`
    already grants -- a remote Tailscale+passkey Board tap never qualifies)
    *and* a chief PTY session is actually alive right now. Without the
    second check, a human driving the Board locally on this same machine
    would get their own session silently marked chief-managed too --
    blocking their own `AskUserQuestion` and rerouting their own
    notifications to chief. Never raises; a marking failure must never fail
    an otherwise-successful dispatch (mirrors `chief_ops.py cmd_dispatch`'s
    own best-effort mark).
    """
    # Imported here, not at module load, so a test's `monkeypatch.setattr
    # (middleware, "LOOPBACK_HOSTS", ...)` is actually observed -- a
    # module-level import would bind this router's own stale copy at import
    # time instead (same reasoning as `_helpers.should_mirror_to_pc`).
    from app.webapp.middleware import LOOPBACK_HOSTS

    if not sid or client_ip(request) not in LOOPBACK_HOSTS:
        return
    try:
        live, _ = await _live_sessions_with_chief_label(cfg)
    except Exception as exc:  # noqa: BLE001 -- best-effort, never fail the dispatch
        logger.debug("chief-managed mark: could not read live sessions: %s", exc)
        return
    if not _find_chief(live):
        return
    try:
        fleet_config = _resolve_repo_entry(cfg, _CHIEF_REPO)
    except HTTPException:
        return
    venv_python = Path(fleet_config.project_dir) / ".venv" / "Scripts" / "python.exe"
    script = Path(fleet_config.project_dir) / "skills" / "_lib" / "chief_managed.py"
    if not venv_python.exists() or not script.exists():
        return
    try:
        await asyncio.to_thread(
            subprocess.run,
            [str(venv_python), str(script), "mark", sid, repo, str(number)],
            capture_output=True,
            timeout=10,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(
            "⚠️ chief-managed mark failed for session %s: %s", sid[:8], exc
        )


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
    module's own ``_find_chief``, ``chief_ops.py``'s worker-cap count and
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
    ``board_spawn._safe_list_sessions`` (``port``-only) — callers that need a
    chief-reconciled live list fetch both and call this, or go through
    :func:`_live_sessions_with_chief_label`.

    Also persists the durable chief pointer (:func:`_note_chief_conversation`)
    here rather than only inside :func:`_live_sessions_with_chief_label` —
    issue #796: ``GET /api/board`` calls this function directly, bypassing
    that wrapper, so a chief typed by hand into a plain Coding-tab session
    (self-healed to ``label="chief"`` by :func:`_reconcile_chief_label` on
    every poll) never got its conversation id written to
    ``webapp/chief-pointer.json``. Both callers now record it; the
    module-level memo in :func:`_note_chief_conversation` keeps this a no-op
    write on every poll after the first for an unchanged chief.
    """
    named = board.attach_shared_names(live, state_rows)
    shared_names = {
        str(item.get("session_id")): item.get("shared_name") for item in named
    }
    reconciled = [
        _reconcile_chief_label(sess, shared_names.get(str(sess.get("session_id"))))
        for sess in live
    ]
    _note_chief_conversation(reconciled, state_rows)
    return reconciled


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
    reconciled = _reconcile_chief_labels(live, state["rows"])
    return reconciled, state


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


# The conversation id last persisted to the pointer sidecar. A module-level
# memo so the steady-state 5s board poll does **zero** file IO (#675): a write
# fires only when the chief's conversation actually changes — about once per
# chief lifetime. Set optimistically, before the write lands: a failed write
# costs one missed pointer refresh, never a write attempt every 5 seconds.
_last_noted_chief_conversation = ""


def _note_chief_conversation(
    live: List[Dict[str, Any]], state_rows: Dict[str, Any]
) -> None:
    """Persist which conversation the live chief is, when that changes (#675).

    This is the write half of the durable pointer (:mod:`src.chief_pointer`).
    It hangs off the *identification* path rather than ``ensure_chief``,
    because the two shapes the pointer exists for are precisely the ones
    ``ensure`` never sees: a chief the launcher never spawned (recognised only
    by :func:`_reconcile_chief_label`'s self-heal) and a chief whose row is
    about to age past ``board.STATE_STALE_AFTER``. Recording the id while the
    PTY is alive is what makes both resumable once it isn't.

    Two properties keep this off ``GET /api/board``'s 5s poll budget: the
    module-level memo above (an unchanged chief does no IO at all) and
    ``create_task`` (the one write per transition runs in a worker thread,
    never on the response path — the same fire-and-forget shape the mirror
    window spawn uses in ``routers/_helpers.py``).

    Silent no-op when there's no live chief, when the claim walk can't resolve
    its conversation, or when that row carries no transcript path — a pointer
    without a transcript can't be substance-checked or resumed, so it is worth
    less than no pointer at all.
    """
    global _last_noted_chief_conversation
    chief = _find_chief(live)
    if not chief:
        return
    conversation = str(
        board.state_sid_for_session(
            live, state_rows, str(chief.get("session_id") or "")
        )
        or ""
    )
    if not conversation or conversation == _last_noted_chief_conversation:
        return
    row = state_rows.get(conversation)
    transcript = row.get("transcript_path") if isinstance(row, dict) else None
    if not transcript:
        return
    _last_noted_chief_conversation = conversation
    asyncio.create_task(
        asyncio.to_thread(
            chief_pointer.write_chief_pointer, conversation, transcript, "live"
        )
    )


def _fresh_fleet_config_stamp(row: Any, now: datetime) -> Optional[datetime]:
    """This ``sessions-state.json`` row's ``updated_at``, if it is a
    live-enough fleet-config row — else ``None``.

    Qualifying means: in the fleet-config checkout (``project``, falling back
    to ``Path(cwd).name`` — the same fallback ``board_sessions._external_row``
    already applies) and stamped within ``board.STATE_STALE_AFTER``. The 24h
    window is applied **per row** rather than trusting the file-level
    ``stale`` flag some *other* row could keep looking fresh: a chief
    conversation that's individually gone cold must not be resumed just
    because a different session kept the file's newest timestamp recent.

    Returns the parsed stamp rather than a bool because every caller that
    cares whether a row qualifies also ranks it by that same stamp.
    """
    if not isinstance(row, dict):
        return None
    cwd = row.get("cwd")
    project = str(row.get("project") or Path(str(cwd or "")).name)
    if project != _CHIEF_PROJECT_DIR_NAME:
        return None
    stamp = board._parse_iso(row.get("updated_at"))
    if stamp is None or now - stamp > board.STATE_STALE_AFTER:
        return None
    return stamp


def _find_resumable_chief_session_id(
    state_rows: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
    preferred_sid: str = "",
) -> str:
    """The chief conversation worth reattaching, or ``""`` (#633, fixed #670).

    Returns the dict key — Claude's own session UUID, exactly what
    ``claude --resume <id>`` needs. Returns ``""`` (never raises) when
    nothing qualifies — same degradation contract every other
    ``board_state`` reader already follows; the caller treats that as "fall
    back to a fresh spawn", not an error.

    **Newest is not the same as right (#670).** The original lookup ranked
    candidates on ``updated_at`` alone, which inverts after any event that
    kills the chief's PTY: the real conversation's row freezes at the moment
    of death, while the blank chief spawned seconds later by the fallback
    path — renamed ``"chief"`` by ``ensure_chief`` itself, so indistinguishable
    by name — keeps writing rows and always wins the recency race. Resume
    then reattached a conversation whose entire content was ``/chief`` and
    the handover file, and it ratcheted: with #640's stop-the-live-chief-first
    ordering, the live blank chief won every subsequent press too, so no
    amount of pressing Resume could get back to the real conversation
    (observed live 2026-07-28: a 95-line, 10-minute-old conversation resumed
    over a 6 468-line one that had been running for three days).

    So substance decides, not just recency. Candidates are ranked in tiers,
    newest-first within each:

    1. ``preferred_sid`` — the conversation of the live chief this resume is
       about to stop, when it has substance. Honors #640's context-preserving
       restart *and* covers a chief the launcher never spawned (its row is
       named after the project, never ``"chief"``, so the name scan below
       cannot see it at all).
    2. Rows named ``"chief"`` (case-insensitive, mirroring ``_CHIEF_TITLE``)
       with a **confirmed** typed human prompt.
    3. The same rows with *unknown* substance — an unreadable transcript, or
       one too big for the tail window to answer over. Unknown is not a
       negative (:func:`board.has_typed_user_prompt`); it just ranks below a
       confirmed one, so a long autonomous chief is still resumable when
       nothing better exists.

    A row whose transcript is *confirmed* to hold no typed prompt is never
    resumed at any tier — reattaching it is worth strictly less than the
    fresh spawn the caller falls back to, which at least re-runs ``/chief``.

    **The durable pointer (#675).** Tiers 2 and 3 above can only ever see what
    ``sessions-state.json`` still holds, and two perfectly resumable chiefs are
    invisible to it: one the launcher never spawned (its row is named after the
    project, so the name scan skips it) once its PTY is gone, and any chief
    dead longer than the hook writer's 24h prune (no row left at all). So the
    remembered conversation (:mod:`src.chief_pointer`, written by
    :func:`_note_chief_conversation` while the chief was alive) joins the scan
    as one more candidate — exempt from the name check and the row-freshness
    gate, since bypassing exactly those two is its purpose, but subject to
    every other rule. It is deliberately **not** a tier of its own: above the
    scan it would override a better live answer, and below it an older
    launcher-spawned row would outrank the newer conversation the pointer
    names. Ranked by its row's ``updated_at`` when that row still exists
    (a live conversation's true recency) and by the pointer's own ``seen_at``
    when it doesn't.
    """
    now = now or board._now()
    if preferred_sid:
        row = state_rows.get(preferred_sid)
        if _fresh_fleet_config_stamp(row, now) is not None and (
            board.has_typed_user_prompt(row.get("transcript_path")) is not False
        ):
            logger.info(
                "↩️ chief resume: reattaching the stopped PTY's own conversation %s",
                str(preferred_sid)[:8],
            )
            return str(preferred_sid)

    # (stamp, sid) of the newest candidate in each substance tier.
    confirmed: Tuple[Optional[datetime], str] = (None, "")
    unknown: Tuple[Optional[datetime], str] = (None, "")

    def consider(sid: str, stamp: datetime, transcript: Any, origin: str) -> None:
        """Rank one candidate into its substance tier, newest-first. The one
        place the "confirmed bootstrap-only is never resumable" rule lives, so
        a state row and the pointer can't drift apart on it."""
        nonlocal confirmed, unknown
        typed = board.has_typed_user_prompt(transcript)
        if typed is False:
            # The #670 breadcrumb: this is the conversation the old
            # recency-only lookup would have handed back.
            logger.info(
                "↩️ chief resume: skipping %s (%s) — bootstrap-only conversation",
                sid[:8], origin,
            )
            return
        tier = confirmed if typed else unknown
        if tier[0] is None or stamp > tier[0]:
            if typed:
                confirmed = (stamp, sid)
            else:
                unknown = (stamp, sid)

    for sid, row in state_rows.items():
        stamp = _fresh_fleet_config_stamp(row, now)
        if stamp is None:
            continue
        if str(row.get("name") or "").strip().lower() != _CHIEF_TITLE:
            continue
        consider(str(sid), stamp, row.get("transcript_path"), "state row")

    pointer = chief_pointer.read_chief_pointer(now=now)
    if pointer:
        pointer_sid = str(pointer.get("session_id") or "")
        pointer_row = state_rows.get(pointer_sid)
        pointer_row = pointer_row if isinstance(pointer_row, dict) else {}
        pointer_stamp = board._parse_iso(
            pointer_row.get("updated_at")
        ) or board._parse_iso(pointer.get("seen_at"))
        if pointer_stamp is not None:
            consider(
                pointer_sid,
                pointer_stamp,
                pointer.get("transcript_path"),
                "pointer",
            )

    chosen = confirmed[1] or unknown[1]
    logger.info(
        "↩️ chief resume: %s",
        f"resuming {chosen[:8]} ({'confirmed' if confirmed[1] else 'unknown'} substance)"
        if chosen
        else "nothing resumable — falling back to a fresh spawn",
    )
    return chosen


def _refresh_chief_pointer_after_resume(
    state_rows: Dict[str, Any], resumed_sid: str
) -> None:
    """Re-stamp the pointer at the conversation a Resume just reattached (#675).

    Blocking IO — callers wrap in ``asyncio.to_thread``; this only ever runs on
    the explicit ensure path, never on the board poll.

    The identification writer would eventually record the same id off the next
    poll, but only once a hook has fired and only while a row still exists.
    Doing it here means the 7-day expiry clock restarts the moment Roberto
    presses Resume — which matters most for exactly the conversation that had
    no row left to find (the transcript path then comes from the pointer that
    named it in the first place). Also primes the memo, so the poll that
    follows doesn't write the same id a second time.
    """
    global _last_noted_chief_conversation
    row = state_rows.get(resumed_sid)
    transcript = row.get("transcript_path") if isinstance(row, dict) else None
    if not transcript:
        pointer = chief_pointer.read_chief_pointer()
        if str(pointer.get("session_id") or "") == resumed_sid:
            transcript = pointer.get("transcript_path")
    if not transcript:
        return
    chief_pointer.write_chief_pointer(resumed_sid, transcript, "ensure-resume")
    _last_noted_chief_conversation = resumed_sid


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
    truthy → reattach the chief conversation worth continuing instead of
    starting fresh (#633): stops any live chief first (same as ``fresh`` —
    never end up with two), looks up the newest *substantive* same-day chief
    conversation in ``sessions-state.json`` for the fleet-config checkout —
    preferring the one the just-stopped PTY was attached to, and never a
    conversation confirmed to hold nothing but its own ``/chief`` bootstrap
    (:func:`_find_resumable_chief_session_id`, #670) — and — if found — spawns with
    ``label="chief"`` declared at spawn time and a direct
    ``claude --resume <id>`` (never the bare interactive picker), skipping the
    ``/chief`` type-in entirely since a resumed conversation is already past
    that point. A chief stopped for a ``resume`` request is thus resumed right
    back into itself — a context-preserving restart, not a coincidence of the
    shared stop-first ordering with ``fresh`` — *unless* that conversation is
    a bootstrap-only one, in which case the substantive conversation it
    displaced wins instead (#670: that is exactly the case where resuming
    "itself" is what loses the real chief). The lookup also consults the
    durable pointer (#675), so a chief the launcher never spawned, or one
    dead past the 24h row-prune, is still reachable. No resumable id (state
    pruned past the 24h window, no prior chief, or nothing but bootstrap-only
    conversations) degrades to today's fresh-spawn-
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
    rows = safe_int(body, "rows", 40)
    cols = safe_int(body, "cols", 120)

    async with _CHIEF_ENSURE_LOCK:
        live, state = await _live_sessions_with_chief_label(cfg)
        chief = _find_chief(live)
        preferred_sid = ""
        if chief:
            sid = str(chief.get("session_id") or "")
            if not fresh and not resume:
                return {"session_id": sid, "spawned": False}
            # Resolve the conversation behind the PTY *before* stopping it —
            # the live list is the only place that join can be made, and a
            # chief the launcher never spawned is invisible to the name-based
            # scan (#670).
            preferred_sid = str(
                board.state_sid_for_session(live, state["rows"], sid) or ""
            )
            await _stop_chief_for_respawn(cfg.session_host_port, sid)

        resumed_session_id = (
            await asyncio.to_thread(
                _find_resumable_chief_session_id,
                state["rows"],
                preferred_sid=preferred_sid,
            )
            if resume
            else ""
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
        session = await spawn_session_or_400(
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

        sid = str(session.get("session_id") or "")
        if resumed_session_id:
            await asyncio.to_thread(
                _refresh_chief_pointer_after_resume,
                state["rows"], resumed_session_id,
            )
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
