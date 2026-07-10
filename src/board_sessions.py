"""Session-claim/merge logic for the Board tab (issue #408 split of ``board.py``).

Joins the **live session list** from the session-host
(``session_client.list_sessions``) with the hook-written state rows
(:mod:`src.board_state`) into board cards. The join is by **normalized cwd**,
never by id: the hook's ``session_id`` is Claude Code's transcript UUID while
the session-host mints its own ``uuid4().hex`` — they can never match. Two
live sessions in one directory tie-break by most recent ``started_at``; the
rest show ``unknown``.

Shared session title (#396): the state row also carries ``name``/``name_source``
(fleet-config#302's live Claude Code session title). :func:`merge_sessions`
copies those onto every card as ``shared_name``/``shared_name_source``, and
:func:`attach_shared_names` runs the identical cwd-based claim walk for the
Coding tab's own ``/api/claude-code/sessions`` list — so a live session
resolves to the same state row, and therefore the same title, on both tabs.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.board_state import STATE_STALE_AFTER, _age_seconds, _now, _parse_iso
from src.board_transcript import _is_working_ghost, _transcript_overlay

logger = logging.getLogger(__name__)

# Statuses the hook writer emits; anything else renders as "unknown".
_KNOWN_STATUSES = frozenset({"working", "needs-you", "idle"})

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _normalize_dir(raw: Any) -> str:
    """Forward slashes, lowercase, no trailing slash — the hook-side rule."""
    return str(raw or "").replace("\\", "/").rstrip("/").lower()


def _match_state_row(
    project_dir_norm: str, unmatched: Dict[str, Dict[str, Any]]
) -> Optional[str]:
    """The unmatched state row whose cwd is equal-or-under the project dir.

    Multiple candidates (several conversations recorded in one directory)
    resolve to the most recently updated row.
    """
    if not project_dir_norm:
        return None
    best_sid: Optional[str] = None
    best_stamp = _EPOCH
    for sid, row in unmatched.items():
        row_cwd = _normalize_dir(row.get("cwd"))
        if row_cwd != project_dir_norm and not row_cwd.startswith(project_dir_norm + "/"):
            continue
        stamp = _parse_iso(row.get("updated_at")) or _EPOCH
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

    for sess in sorted(live, key=started, reverse=True):
        sid = _match_state_row(_normalize_dir(sess.get("project_dir")), unmatched)
        pairs.append((sess, unmatched.pop(sid) if sid else None, sid))
    return pairs, unmatched


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

    Uses the exact same cwd-based :func:`_claim_walk` as :func:`merge_sessions`
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

    Live sessions are walked newest-first so the freshest session in a shared
    directory claims that directory's state row; later (older) sessions in the
    same directory render ``unknown``. Fresh state rows with no live match
    become state-only cards (a conversation running in a plain desktop
    terminal): ``alive: False``, no ``session_id`` the terminal could attach to.
    Waiting statuses are checked against transcript activity — see
    :func:`src.board_transcript._transcript_overlay` (#305).

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
        status, anchor = _transcript_overlay(row, status, anchor)
        cards.append({
            "session_id": sess.get("session_id"),
            "state_sid": sid,
            "kind": sess.get("kind"),
            "agent": sess.get("agent"),
            "project_dir": project_dir,
            "name": sess.get("name"),
            "alive": bool(sess.get("alive", True)),
            "started_at": sess.get("started_at"),
            "live_title": sess.get("live_title") or "",
            "prompt_title": sess.get("prompt_title") or "",
            "shared_name": (row or {}).get("name"),
            "shared_name_source": (row or {}).get("name_source"),
            "project": (row or {}).get("project") or Path(str(project_dir or "")).name,
            "status": status,
            "age_seconds": _age_seconds(anchor, now),
        })

    for sid, row in unmatched.items():
        stamp = _parse_iso(row.get("updated_at"))
        if stamp is None or now - stamp > STATE_STALE_AFTER:
            continue  # cold leftovers: not worth a card
        raw_status = row.get("status")
        cwd = row.get("cwd")
        project = row.get("project") or Path(str(cwd or "")).name
        status = raw_status if raw_status in _KNOWN_STATUSES else "unknown"
        status, anchor = _transcript_overlay(row, status, stamp)
        if _is_working_ghost(row, status, now):
            continue  # dead headless/sdk-cli session — see #322
        cards.append({
            "session_id": None,
            "kind": "external",
            "agent": "claude",
            "project_dir": cwd,
            "name": str(project),
            "alive": False,
            "started_at": None,
            "live_title": "",
            "prompt_title": "",
            "shared_name": row.get("name"),
            "shared_name_source": row.get("name_source"),
            "project": str(project),
            "status": status,
            "age_seconds": _age_seconds(anchor, now),
        })

    return cards
