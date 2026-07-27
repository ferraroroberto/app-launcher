"""Session-claim/merge logic for the Board tab (issue #408 split of ``board.py``).

Joins the **live session list** from the session-host
(``session_client.list_sessions``) with the hook-written state rows
(:mod:`src.board_state`) into board cards. The join is exact launcher id +
agent when a writer supplies those fields; legacy Claude rows fall back to an
agent-gated normalized-cwd claim. Two legacy live sessions in one directory
tie-break by most recent ``started_at``; the rest show ``unknown``.

Shared session title (#396): the state row also carries ``name``/``name_source``
(fleet-config#302's live Claude Code session title). :func:`merge_sessions`
copies those onto every card as ``shared_name``/``shared_name_source``, and
:func:`attach_shared_names` runs the identical agent-aware claim walk for the
Coding tab's own ``/api/claude-code/sessions`` list — so a live session
resolves to the same state row, and therefore the same title, on both tabs.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.board_state import STATE_STALE_AFTER, _age_seconds, _now, _parse_iso
from src.board_transcript import (
    _external_row_liveness,
    _refine_waiting_status,
    _transcript_overlay,
)

logger = logging.getLogger(__name__)

# Statuses the hook writer emits; anything else renders as "unknown". This
# gates the *raw* row status only — a card's final ``status`` can diverge
# further downstream (the transcript overlay, and #608's needs-you split;
# see :func:`merge_sessions`).
_KNOWN_STATUSES = frozenset({"working", "needs-you", "idle"})

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)

# One breadcrumb per rejected state id per webapp process. GET /api/board polls
# every five seconds; logging every rejection on every poll would bury the
# useful diagnosis in noise. Bound the set so a very long-lived tray cannot
# accumulate ids without limit.
_LOGGED_SUPPRESSED_ROWS: set[str] = set()
_SUPPRESSED_LOG_CAP = 512


def _normalize_dir(raw: Any) -> str:
    """Forward slashes, lowercase, no trailing slash — the hook-side rule."""
    return str(raw or "").replace("\\", "/").rstrip("/").lower()


def _row_agent(row: Dict[str, Any]) -> str:
    """State rows predating #455 are Claude Code rows by definition."""
    return str(row.get("agent") or "claude").strip().lower()


def _session_agent(session: Dict[str, Any]) -> str:
    return str(session.get("agent") or "claude").strip().lower()


def _cwd_agent_key(session: Dict[str, Any]) -> tuple:
    """(normalized project dir, agent) — the cwd fallback's matching grain."""
    return (_normalize_dir(session.get("project_dir")), _session_agent(session))


def _match_state_row(
    session: Dict[str, Any],
    unmatched: Dict[str, Dict[str, Any]],
    *,
    ambiguous_cwd: bool = False,
) -> Optional[str]:
    """Claim an agent-compatible state row for one live session.

    A writer-provided ``launcher_session_id`` is exact and wins first. Legacy
    Claude rows have neither that field nor ``agent`` and retain the normalized
    cwd fallback. Rows carrying a different launcher id are never allowed to
    fall back by cwd — that would reintroduce the same-session collision exact
    identity exists to prevent (#455).

    The cwd fallback also requires the row's ``updated_at`` to be at-or-after
    the session's own ``started_at`` (#482) — a row genuinely written by this
    session can never predate the session's existence, so an older row can
    only be some other, unrelated conversation's leftover state in the same
    directory (e.g. a race where this session's own row hasn't landed yet).
    Without this guard the "most recently updated" tie-break could hand a
    brand-new session an hours-old sibling's title.

    ``ambiguous_cwd`` (#537): true when 2+ *live* sessions share this
    session's (cwd, agent) — set by :func:`_claim_walk` from the full live
    list, not just the rows still unclaimed. With only recency to go on, the
    cwd fallback has no way to tell which of several live siblings a given
    row actually belongs to; picking the highest-``updated_at`` one anyway
    cross-wires cards to the wrong session's transcript (reproduced live: 3
    concurrent sessions in one directory, all 3 assigned to each other's
    rows). Degrading to no match (``unknown`` card, no transcript) is safer
    than a confident wrong answer — same principle as the Codex ambiguity
    guard in :func:`src.board_exchange._find_codex_transcript`. Sessions that
    carry their own ``launcher_session_id`` (the exact-match path above)
    never reach here, so this only affects legacy/external sessions with no
    launcher id sharing a directory.
    """
    session_id = str(session.get("session_id") or "")
    agent = _session_agent(session)
    exact_sid: Optional[str] = None
    exact_stamp = _EPOCH
    for sid, row in unmatched.items():
        launcher_sid = str(row.get("launcher_session_id") or "")
        if launcher_sid and launcher_sid == session_id and _row_agent(row) == agent:
            stamp = _parse_iso(row.get("updated_at")) or _EPOCH
            if exact_sid is None or stamp > exact_stamp:
                exact_sid, exact_stamp = sid, stamp
    if exact_sid is not None:
        return exact_sid

    if ambiguous_cwd:
        return None

    project_dir_norm = _normalize_dir(session.get("project_dir"))
    if not project_dir_norm:
        return None
    session_started = _parse_iso(session.get("started_at")) or _EPOCH
    best_sid: Optional[str] = None
    best_stamp = _EPOCH
    for sid, row in unmatched.items():
        if _row_agent(row) != agent or row.get("launcher_session_id"):
            continue
        row_cwd = _normalize_dir(row.get("cwd"))
        if row_cwd != project_dir_norm and not row_cwd.startswith(project_dir_norm + "/"):
            continue
        stamp = _parse_iso(row.get("updated_at")) or _EPOCH
        if stamp < session_started:
            continue
        if best_sid is None or stamp > best_stamp:
            best_sid, best_stamp = sid, stamp
    return best_sid


def _claim_walk(
    live: List[Dict[str, Any]], state_rows: Dict[str, Dict[str, Any]]
) -> tuple:
    """Assign state rows to live sessions, newest session first.

    The single source of the claim order — ``merge_sessions`` renders it and
    ``state_row_for_session`` resolves one session's row consistently with
    what the board displays. Returns ``(pairs, leftovers)`` where ``pairs``
    is ``[(session, row-or-None, sid-or-None), ...]`` (fleet-config#242's
    ``state_sid`` — the claimed row's own key — rides alongside the row so
    ``merge_sessions`` can put it on the card) and ``leftovers`` the unclaimed
    rows.
    """
    unmatched = dict(state_rows)
    pairs: List[tuple] = []

    def started(sess: Dict[str, Any]) -> datetime:
        return _parse_iso(sess.get("started_at")) or _EPOCH

    # Counted over the FULL live list (not the shrinking unmatched-rows
    # walk) — the ambiguity is about how many live sessions are competing
    # for this directory, independent of claim order.
    cwd_agent_counts = Counter(_cwd_agent_key(sess) for sess in live)

    for sess in sorted(live, key=started, reverse=True):
        ambiguous = cwd_agent_counts[_cwd_agent_key(sess)] > 1
        sid = _match_state_row(sess, unmatched, ambiguous_cwd=ambiguous)
        pairs.append((sess, unmatched.pop(sid) if sid else None, sid))
    return pairs, unmatched


def _claimed_transcript_paths(pairs: List[tuple]) -> "set[str]":
    """Transcript paths already backing a live, matched session's own row
    (#613) — an unmatched row pointing at the same file is a superseded
    leftover of that same session (re-keyed when a worker moved from one
    issue to the next), not independent evidence of a second live process."""
    claimed: "set[str]" = set()
    for sess, row, _sid in pairs:
        if not sess.get("alive") or not row:
            continue
        path = row.get("transcript_path")
        if path:
            claimed.add(path)
    return claimed


def _live_launcher_session_ids(live: List[Dict[str, Any]]) -> "set[str]":
    """Every currently-alive session-host id (#613) — the session-host is the
    authority on which PTYs it owns, so a hook row whose own
    ``launcher_session_id`` is absent from this set is provably dead, however
    recently its transcript happened to be touched."""
    return {
        str(sess.get("session_id"))
        for sess in live
        if sess.get("alive") and sess.get("session_id")
    }


def state_row_for_session(
    live: List[Dict[str, Any]],
    state_rows: Dict[str, Dict[str, Any]],
    session_id: str,
) -> Optional[Dict[str, Any]]:
    """The state row the board's merge assigns to this live session (or None).

    Used by the drill-down endpoint (#301) to find a session's
    ``transcript_path`` — the transcript UUID lives only in the hook row,
    never in the session-host record.
    """
    pairs, _ = _claim_walk(live, state_rows)
    for sess, row, _sid in pairs:
        if str(sess.get("session_id")) == str(session_id):
            return row
    return None


def attach_shared_names(
    live: List[Dict[str, Any]], state_rows: Dict[str, Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Join live session-host sessions with the state file's shared session
    title (fleet-config#302's ``name``/``name_source``), for the Coding tab's
    Running-sessions list (#396).

    Uses the exact same agent-aware :func:`_claim_walk` as :func:`merge_sessions`
    — both consumers must resolve a given live session to the same state row,
    or the Board tab and the Coding tab could show two different titles for
    the same session. Returns new dicts (each session's own fields plus
    ``shared_name``/``shared_name_source``, both ``None`` on no match) — the
    input dicts are never mutated.
    """
    pairs, _ = _claim_walk(live, state_rows)
    return [
        {
            **sess,
            "shared_name": (row or {}).get("name"),
            "shared_name_source": (row or {}).get("name_source"),
        }
        for sess, row, _sid in pairs
    ]


def merge_sessions(
    live: List[Dict[str, Any]],
    state_rows: Dict[str, Dict[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Join live session-host sessions with hook state rows into board cards.

    Live sessions are walked newest-first. Exact launcher-session id + agent
    claims win; legacy Claude rows fall back to cwd recency. Later sessions in
    the same directory render ``unknown``. A state row with no live match only
    becomes an external card when recent transcript activity independently
    proves the process still exists — hook state alone is not liveness (#455).
    Waiting statuses are checked against transcript activity — see
    :func:`src.board_transcript._transcript_overlay` (#305) — and a hook
    ``needs-you`` is then split into ``stalled`` / ``awaiting-decision`` /
    ``idle-finished`` / ``awaiting-input`` (#608's
    :func:`src.board_transcript._refine_waiting_status`) so a caller never
    has to fetch the exchange to tell those four apart. The raw
    ``needs-you`` string itself never reaches a card's ``status`` field.

    Live-session cards also carry ``state_sid`` — the claimed state row's own
    key (``None`` when unmatched) — so a Slack ping's deep link, which only
    knows the hook's transcript UUID, can still resolve to the right card
    (fleet-config#242 / #307). State-only cards don't get one: they have no
    session-host id and no drawer target, so a deep link to one is out of scope.
    """
    now = now or _now()
    cards: List[Dict[str, Any]] = []
    pairs, unmatched = _claim_walk(live, state_rows)

    for sess, row, sid in pairs:
        project_dir = sess.get("project_dir")

        raw_status = (row or {}).get("status")
        status = raw_status if raw_status in _KNOWN_STATUSES else "unknown"
        anchor = (_parse_iso(row.get("updated_at")) if row else None) or (
            _parse_iso(sess.get("started_at"))
        )
        status, anchor = _transcript_overlay(row, status, anchor, now=now)
        status = _refine_waiting_status(status, (row or {}).get("transcript_path"))
        cards.append({
            "session_id": sess.get("session_id"),
            "state_sid": sid,
            "kind": sess.get("kind"),
            "agent": sess.get("agent"),
            "label": sess.get("label") or "",
            "project_dir": project_dir,
            "name": sess.get("name"),
            "alive": bool(sess.get("alive", True)),
            "started_at": sess.get("started_at"),
            "live_title": sess.get("live_title") or "",
            "prompt_title": sess.get("prompt_title") or "",
            "manual_title": sess.get("manual_title") or "",
            "shared_name": (row or {}).get("name"),
            "shared_name_source": (row or {}).get("name_source"),
            "project": (row or {}).get("project") or Path(str(project_dir or "")).name,
            "status": status,
            "age_seconds": _age_seconds(anchor, now),
        })

    claimed_transcripts = _claimed_transcript_paths(pairs)
    live_launcher_session_ids = _live_launcher_session_ids(live)

    for sid, row in unmatched.items():
        stamp = _parse_iso(row.get("updated_at"))
        if stamp is None or now - stamp > STATE_STALE_AFTER:
            continue  # cold leftovers: not worth a card
        raw_status = row.get("status")
        cwd = row.get("cwd")
        project = row.get("project") or Path(str(cwd or "")).name
        status = raw_status if raw_status in _KNOWN_STATUSES else "unknown"
        status, anchor = _transcript_overlay(row, status, stamp, now=now)
        status = _refine_waiting_status(status, row.get("transcript_path"))
        externally_live, reason = _external_row_liveness(
            row, now,
            claimed_transcripts=claimed_transcripts,
            live_launcher_session_ids=live_launcher_session_ids,
        )
        if not externally_live:
            if sid not in _LOGGED_SUPPRESSED_ROWS:
                if len(_LOGGED_SUPPRESSED_ROWS) >= _SUPPRESSED_LOG_CAP:
                    _LOGGED_SUPPRESSED_ROWS.clear()
                _LOGGED_SUPPRESSED_ROWS.add(sid)
                logger.info(
                    "ℹ️ Board suppressed unverifiable state row %s (%s, %s): %s",
                    sid[:8], project, status, reason,
                )
            continue
        cards.append({
            "session_id": None,
            "kind": "external",
            "agent": _row_agent(row),
            "project_dir": cwd,
            "name": str(project),
            "alive": False,
            "started_at": None,
            "live_title": "",
            "prompt_title": "",
            "manual_title": "",
            "shared_name": row.get("name"),
            "shared_name_source": row.get("name_source"),
            "project": str(project),
            "status": status,
            "age_seconds": _age_seconds(anchor, now),
        })

    return cards
