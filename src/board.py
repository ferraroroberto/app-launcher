"""Pure assembly logic for the Board tab's kanban columns (issue #300 / #164).

Three inputs, one board:

* the **live session list** from the session-host (``session_client.list_sessions``),
* the **sessions-state file** written by fleet-config's ``session_state`` hook
  (``~/.claude/hooks/state/sessions-state.json`` — ``working`` / ``needs-you`` /
  ``idle`` rows per recent Claude Code session),
* the **jobs attention scan** (failed-today / stuck runs from ``src.jobs``),

plus the in-memory GitHub snapshot from :mod:`src.github_client`.

The join between hook rows and live sessions is by **normalized cwd**, never by
id: the hook's ``session_id`` is Claude Code's transcript UUID while the
session-host mints its own ``uuid4().hex`` — they can never match. Two live
sessions in one directory tie-break by most recent ``started_at``; the rest
show ``unknown``. Everything here is pure (inputs → dicts, injectable clock),
so it unit-tests without a webapp.

Degradation contract (#164 acceptance): a missing/corrupt/stale state file
must never error — session cards fall back to ``unknown`` status and the
GitHub/jobs columns render regardless.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# A state row (or the whole file) older than this is treated as gone-cold —
# matches the writer's own prune horizon in fleet-config's session_state hook.
STATE_STALE_AFTER = timedelta(hours=24)

# Statuses the hook writer emits; anything else renders as "unknown".
_KNOWN_STATUSES = frozenset({"working", "needs-you", "idle"})

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_dir(raw: Any) -> str:
    """Forward slashes, lowercase, no trailing slash — the hook-side rule."""
    return str(raw or "").replace("\\", "/").rstrip("/").lower()


def _parse_iso(raw: Any) -> Optional[datetime]:
    """Tolerant ISO-8601 parse; naive stamps are assumed local (job records)."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def _iso_z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _age_seconds(anchor: Optional[datetime], now: datetime) -> Optional[int]:
    if anchor is None:
        return None
    return max(0, int((now - anchor).total_seconds()))


# ------------------------------------------------------------ state file


def read_sessions_state(path: Path, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Read the hook-written state file with full staleness tolerance.

    Absent / unreadable / corrupt → ``{"available": False, ...}`` with empty
    rows (precedent: ``life_os.recap_status`` and ``jobs._read_queue_file``).
    ``stale`` is true when the newest row is older than
    :data:`STATE_STALE_AFTER` — the hooks have stopped writing.
    """
    now = now or _now()
    empty: Dict[str, Any] = {"available": False, "stale": False, "updated_at": None, "rows": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty

    rows = {
        str(sid): row for sid, row in data.items() if isinstance(row, dict)
    }
    stamps = [
        stamp
        for stamp in (_parse_iso(row.get("updated_at")) for row in rows.values())
        if stamp is not None
    ]
    newest = max(stamps) if stamps else None
    return {
        "available": True,
        "stale": bool(newest is not None and now - newest > STATE_STALE_AFTER),
        "updated_at": _iso_z(newest) if newest else None,
        "rows": rows,
    }


# --------------------------------------------------------- session merge


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
    is ``[(session, row-or-None), ...]`` and ``leftovers`` the unclaimed rows.
    """
    unmatched = dict(state_rows)
    pairs: List[tuple] = []

    def started(sess: Dict[str, Any]) -> datetime:
        return _parse_iso(sess.get("started_at")) or _EPOCH

    for sess in sorted(live, key=started, reverse=True):
        sid = _match_state_row(_normalize_dir(sess.get("project_dir")), unmatched)
        pairs.append((sess, unmatched.pop(sid) if sid else None))
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
    for sess, row in pairs:
        if str(sess.get("session_id")) == str(session_id):
            return row
    return None


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
    """
    now = now or _now()
    cards: List[Dict[str, Any]] = []
    pairs, unmatched = _claim_walk(live, state_rows)

    for sess, row in pairs:
        project_dir = sess.get("project_dir")

        raw_status = (row or {}).get("status")
        status = raw_status if raw_status in _KNOWN_STATUSES else "unknown"
        anchor = (_parse_iso(row.get("updated_at")) if row else None) or (
            _parse_iso(sess.get("started_at"))
        )
        cards.append({
            "session_id": sess.get("session_id"),
            "kind": sess.get("kind"),
            "agent": sess.get("agent"),
            "project_dir": project_dir,
            "name": sess.get("name"),
            "alive": bool(sess.get("alive", True)),
            "started_at": sess.get("started_at"),
            "live_title": sess.get("live_title") or "",
            "prompt_title": sess.get("prompt_title") or "",
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
            "project": str(project),
            "status": raw_status if raw_status in _KNOWN_STATUSES else "unknown",
            "age_seconds": _age_seconds(stamp, now),
        })

    return cards


# -------------------------------------------------------- last exchange

# Only the transcript's tail is read — a long session's JSONL runs to many
# MB but the last exchange always sits within the final few hundred KB.
_EXCHANGE_TAIL_BYTES = 256 * 1024

# User lines whose string content is harness plumbing, not a typed prompt:
# slash-command wrappers, local-command output, background-task events.
_SKIP_USER_PREFIXES = (
    "<command-", "<local-command-", "<task-notification", "<system-reminder",
)

# Phone-drawer display caps — the ⚡ open-terminal button is the escape
# hatch for anything longer.
_ASSISTANT_TEXT_CAP = 6000
_USER_TEXT_CAP = 1500


def _assistant_text(content: Any) -> str:
    """Join the ``text`` blocks of an assistant message's content list."""
    if not isinstance(content, list):
        return ""
    parts = [
        str(block.get("text") or "").strip()
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n\n".join(p for p in parts if p)


def last_exchange(transcript_path: Any) -> Dict[str, Any]:
    """The last completed user→assistant exchange from a transcript JSONL.

    Reads the final :data:`_EXCHANGE_TAIL_BYTES`, walks the lines in reverse:
    the newest assistant line carrying a ``text`` block is the reply (earlier
    lines of the *same* ``message.id`` are prepended — transcripts write one
    line per content block); the nearest preceding user line whose content is
    a plain string is the prompt (list-shaped user content is tool results;
    harness wrappers like ``<command-…>`` are skipped). Missing file, no
    assistant text in the tail → ``{"available": False}`` — never an error.
    """
    unavailable: Dict[str, Any] = {"available": False, "user": None, "assistant": None}
    if not transcript_path:
        return unavailable
    try:
        with Path(str(transcript_path)).open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - _EXCHANGE_TAIL_BYTES))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return unavailable
    if size > _EXCHANGE_TAIL_BYTES and lines:
        lines = lines[1:]  # first line is almost certainly a partial record

    assistant: Optional[Dict[str, Any]] = None
    assistant_msg_id: Any = None
    user: Optional[Dict[str, Any]] = None

    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}

        if assistant is None:
            if obj.get("type") != "assistant":
                continue
            text = _assistant_text(msg.get("content"))
            if not text:
                continue  # thinking / tool_use-only line
            assistant = {"text": text, "timestamp": obj.get("timestamp")}
            assistant_msg_id = msg.get("id")
            continue

        if (
            obj.get("type") == "assistant"
            and assistant_msg_id
            and msg.get("id") == assistant_msg_id
        ):
            text = _assistant_text(msg.get("content"))
            if text:
                assistant["text"] = text + "\n\n" + assistant["text"]
                assistant["timestamp"] = obj.get("timestamp") or assistant["timestamp"]
            continue

        if obj.get("type") == "user":
            content = msg.get("content")
            if not isinstance(content, str):
                continue  # tool results ride as content lists
            stripped = content.strip()
            if not stripped or any(
                stripped.startswith(p) for p in _SKIP_USER_PREFIXES
            ):
                continue
            user = {
                "text": stripped[:_USER_TEXT_CAP],
                "timestamp": obj.get("timestamp"),
            }
            break

    if assistant is None:
        return unavailable
    assistant["text"] = assistant["text"][-_ASSISTANT_TEXT_CAP:]
    return {"available": True, "user": user, "assistant": assistant}


# ------------------------------------------------------------------ jobs


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


# ----------------------------------------------------------------- board


def build_board(
    session_cards: List[Dict[str, Any]],
    github: Dict[str, Any],
    job_cards: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Route the three sources into the four computed columns.

    Backlog = open issues. Claude's turn = sessions working / unknown / idle
    (idle is dimmed client-side, not hidden — an idle session is still Claude
    holding a workspace). Your turn = needs-you sessions first, then open PRs,
    then failed/stuck jobs. Done = today's merged PRs + closed issues.
    """
    claude_turn = [c for c in session_cards if c["status"] != "needs-you"]
    your_turn = [c for c in session_cards if c["status"] == "needs-you"]
    return {
        "backlog": list(github.get("issues") or []),
        "claude_turn": claude_turn,
        "your_turn": your_turn + list(github.get("prs") or []) + job_cards,
        "done": list(github.get("done") or []),
    }
