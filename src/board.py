"""Assembly logic for the Board tab's kanban columns (issue #300 / #164).

Five single-purpose columns (#399): Backlog and Done hold issues only;
Claude's turn and Your turn hold session cards only; Other holds everything
else that needs attention but isn't a terminal or an issue — open PRs and
failed/stuck jobs. See :func:`build_board` for the exact routing.

Three inputs, one board:

* the **live session list** from the session-host (``session_client.list_sessions``),
* the **sessions-state file** written by fleet-config's ``session_state`` hook
  (``~/.claude/hooks/state/sessions-state.json`` — ``working`` / ``needs-you``
  / ``idle`` rows per recent Claude Code session),
* the **jobs attention scan** (failed-today / stuck runs from ``src.jobs``),

plus the in-memory GitHub snapshot from :mod:`src.github_client`.

This module is now a thin ``build_board`` assembly plus the jobs-attention
scan (issue #408 split — was a 690-line god file). The three other concerns
each own their own module, and their public names are re-exported here so
every existing ``board.<name>`` call site (routers, tests) keeps working
unchanged:

* :mod:`src.board_state` — hook-state-file IO (:func:`read_sessions_state`,
  :func:`read_active_issues`, :func:`read_rate_limits`).
* :mod:`src.board_sessions` — session-claim/merge logic
  (:func:`merge_sessions`, :func:`attach_shared_names`,
  :func:`state_row_for_session`). Writer-provided launcher id + agent wins;
  legacy Claude rows use an agent-gated normalized-cwd fallback.
* :mod:`src.board_transcript` — transcript-JSONL parsing
  (:func:`last_exchange`, plus the activity-overlay/external-liveness helpers
  ``merge_sessions`` calls internally).

Degradation contract (#164 acceptance): a missing/corrupt/stale state file
must never error — session cards fall back to ``unknown`` status and the
GitHub/jobs columns render regardless.

Shared session title (#396): the state row also carries ``name``/``name_source``
(fleet-config#302's live Claude Code session title). ``merge_sessions`` copies
those onto every card as ``shared_name``/``shared_name_source``, and
:func:`attach_shared_names` runs the identical agent-aware claim walk for the
Coding tab's own ``/api/claude-code/sessions`` list — so a live session
resolves to the same state row, and therefore the same title, on both tabs.
The frontend's precedence lives in one place, ``sessions.js``'s
``sessionTitle()``, which both tabs call.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.board_state import (  # noqa: F401 — re-exported for board.<name> callers
    RATE_LIMITS_STALE_AFTER,
    STATE_STALE_AFTER,
    _age_seconds,
    _now,
    _parse_iso,
    read_active_issues,
    read_rate_limits,
    read_sessions_state,
)
from src.board_sessions import (  # noqa: F401 — re-exported for board.<name> callers
    attach_shared_names,
    merge_sessions,
    state_row_for_session,
)
from src.board_transcript import last_exchange  # noqa: F401 — re-exported

logger = logging.getLogger(__name__)


def jobs_attention(*, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Failed-today and stuck runs across all registered jobs.

    Blocking file IO (one ``list_runs`` walk per job) — callers wrap in
    ``asyncio.to_thread``. Job timestamps are naive local ISO strings
    (``run_job_cmd`` writes ``datetime.now().isoformat()``), so "today" is the
    local calendar day.
    """
    from src import jobs as jobs_mod
    from src.jobs_config import load_jobs

    now_local = (now or _now()).astimezone()
    today = now_local.date()
    cards: List[Dict[str, Any]] = []

    for job in load_jobs().jobs:
        try:
            latest = jobs_mod.latest_run(job.id)
        except OSError:
            continue
        if not latest:
            continue
        if latest.get("status") == "running" and jobs_mod.is_stuck(job.id):
            started = _parse_iso(latest.get("started_at"))
            cards.append({
                "kind": "job",
                "job_id": job.id,
                "job_name": job.name,
                "state": "stuck",
                "run_id": latest.get("run_id"),
                "finished_at": None,
                "age_seconds": _age_seconds(started, now_local),
            })
            continue
        if latest.get("status") == "failed":
            finished = _parse_iso(latest.get("finished_at"))
            if finished is not None and finished.astimezone().date() == today:
                cards.append({
                    "kind": "job",
                    "job_id": job.id,
                    "job_name": job.name,
                    "state": "failed",
                    "run_id": latest.get("run_id"),
                    "finished_at": latest.get("finished_at"),
                    "age_seconds": _age_seconds(finished, now_local),
                })

    return cards


def _is_chief_card(card: Dict[str, Any]) -> bool:
    """Board-side mirror of the frontend's ``isChiefSession`` (dom-utils.js) —
    label-first, ``kind``/``name`` fallback for a session-host predating the
    label field. Kept in lockstep with that JS predicate (#575)."""
    return bool(
        card.get("label") == "chief"
        or (card.get("kind") == "pty" and card.get("name") == "chief")
    )


def build_board(
    session_cards: List[Dict[str, Any]],
    github: Dict[str, Any],
    job_cards: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Route the three sources into the five computed columns.

    Each column now holds one kind of card (#399): Backlog = open issues.
    Claude's turn = sessions working / unknown / idle (idle is dimmed
    client-side, not hidden — an idle session is still Claude holding a
    workspace). Your turn = needs-you sessions only — a terminal that needs a
    human. Other = everything else that needs attention but isn't a terminal:
    open PRs, then failed/stuck jobs. Done = today's closed issues only — a
    merged PR that closed one is already reflected by the issue itself.

    The standing fleet chief (#245) never routes to Your turn (#575): its
    ``Stop``-hook-driven ``needs-you`` status just means "chief finished
    replying," which for a long-lived chat session is true for nearly its
    entire life between dispatches, not a transient alert like it is for a
    worker session. It always lands in Claude's turn instead, regardless of
    its raw hook status.
    """
    claude_turn = [
        c for c in session_cards if c["status"] != "needs-you" or _is_chief_card(c)
    ]
    your_turn = [
        c for c in session_cards if c["status"] == "needs-you" and not _is_chief_card(c)
    ]
    return {
        "backlog": list(github.get("issues") or []),
        "claude_turn": claude_turn,
        "your_turn": your_turn,
        "other": list(github.get("prs") or []) + job_cards,
        "done": list(github.get("done") or []),
    }
