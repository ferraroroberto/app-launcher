"""Pure assembly logic for the Board tab's kanban columns (issue #300 / #164).

Five single-purpose columns (#399): Backlog and Done hold issues only;
Claude's turn and Your turn hold session cards only; Other holds everything
else that needs attention but isn't a terminal or an issue — open PRs and
failed/stuck jobs. See :func:`build_board` for the exact routing.

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
show ``unknown``. Everything here is pure (inputs → dicts, injectable clock)
— except one ``os.stat`` (plus a bounded tail read when the stat says the file
moved, #305/#309) per session row for the transcript-activity overlay — so it
unit-tests without a webapp.

Degradation contract (#164 acceptance): a missing/corrupt/stale state file
must never error — session cards fall back to ``unknown`` status and the
GitHub/jobs columns render regardless.

Shared session title (#396): the state row also carries ``name``/``name_source``
(fleet-config#302's live Claude Code session title). ``merge_sessions`` copies
those onto every card as ``shared_name``/``shared_name_source``, and
:func:`attach_shared_names` runs the identical cwd-based claim walk for the
Coding tab's own ``/api/claude-code/sessions`` list — so a live session
resolves to the same state row, and therefore the same title, on both tabs.
The frontend's precedence lives in one place, ``sessions.js``'s
``sessionTitle()``, which both tabs call.
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

# The rate-limits cache (issue #326) is meant to read as near-real-time — the
# statusline that writes it re-renders on every assistant message — so this
# is far shorter than STATE_STALE_AFTER's 24h (that one tolerates a cold
# session; a usage percentage sitting stale for hours would be misleading).
RATE_LIMITS_STALE_AFTER = timedelta(minutes=10)

# Statuses the hook writer emits; anything else renders as "unknown".
_KNOWN_STATUSES = frozenset({"working", "needs-you", "idle"})

# Transcript-activity overlay (#305): a transcript appended this much later
# than the row's stamp means Claude resumed without any hook firing. The
# margin absorbs the Stop hook and the final transcript write landing a
# couple of seconds apart in either order.
_RESUME_EPSILON = timedelta(seconds=10)

# Working-ghost detection (#322): headless / Agent-SDK-CLI invocations (cron
# routines, sub-agent bootstraps) apparently never fire Stop or SessionEnd, so
# their row sticks at "working" forever instead of being deleted or flipped to
# needs-you. A transcript quiet this much longer than any real turn takes
# means the process is dead, not working.
_WORKING_GHOST_AFTER = timedelta(minutes=15)

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


# ------------------------------------------------------- read_rate_limits


def _rate_limit_window(raw: Any) -> Optional[Dict[str, Any]]:
    """One ``five_hour``/``seven_day`` window, tolerant field-by-field.

    ``raw`` may be absent, ``null``, or a dict with either sub-field missing
    or ``null`` — a future writer may report a window before it has learned
    ``resets_at``, say. Any shape short of "a dict" collapses to ``None``
    (window not reported); within a dict, each sub-field is pulled
    independently so one bad field doesn't blank the other.
    """
    if not isinstance(raw, dict):
        return None
    pct = raw.get("used_percentage")
    used_percentage = pct if isinstance(pct, (int, float)) else None
    resets_at: Optional[int] = None
    raw_resets = raw.get("resets_at")
    if isinstance(raw_resets, (int, float)):
        resets_at = int(raw_resets)
    return {"used_percentage": used_percentage, "resets_at": resets_at}


def read_rate_limits(path: Path, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The cached 5h/7d Claude usage percentages a statusline writer maintains.

    Mirrors :func:`read_sessions_state`'s degradation contract exactly:
    absent/corrupt/non-dict file → ``available: False`` with both windows
    ``None``, never an error. ``stale`` is driven by the file's own
    ``captured_at`` stamp (when the writer last ran) against
    :data:`RATE_LIMITS_STALE_AFTER` — *not* by either window's ``resets_at``,
    which is the reset moment, not a freshness signal.
    """
    now = now or _now()
    empty: Dict[str, Any] = {
        "available": False,
        "stale": False,
        "updated_at": None,
        "five_hour": None,
        "seven_day": None,
    }
    try:
        # utf-8-sig: the statusline writer is an external PowerShell script
        # we don't fully control, and .NET's UTF8Encoding can default to
        # emitting a BOM — utf-8-sig strips one if present and reads plain
        # utf-8 identically otherwise, so a BOM never masquerades as a
        # corrupt file.
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty

    captured_at = _parse_iso(data.get("captured_at"))
    return {
        "available": True,
        "stale": bool(
            captured_at is not None and now - captured_at > RATE_LIMITS_STALE_AFTER
        ),
        "updated_at": _iso_z(captured_at) if captured_at else None,
        "five_hour": _rate_limit_window(data.get("five_hour")),
        "seven_day": _rate_limit_window(data.get("seven_day")),
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


_ACTIVITY_TAIL_BYTES = 8 * 1024


def _last_activity(transcript_path: Any) -> Optional[datetime]:
    """Timestamp of the newest real conversation event in the transcript tail.

    Claude Code appends non-message metadata lines (``system``, ``pr-link``,
    ``ai-title``, ``file-history-snapshot``, …) seconds-to-minutes after a
    turn ends, so the file's mtime overstates activity (#309). Only
    ``assistant``/``user`` lines carrying a ``message`` payload mark a live
    turn; the newest one's ``timestamp`` is the activity anchor. Torn or
    unparseable lines are skipped (a line may be appended mid-read); no
    conversation event in the tail → ``None`` — callers keep the hook status.
    """
    try:
        with Path(str(transcript_path)).open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - _ACTIVITY_TAIL_BYTES))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("type") not in ("assistant", "user"):
            continue
        if not isinstance(obj.get("message"), dict):
            continue
        stamp = _parse_iso(obj.get("timestamp"))
        if stamp is not None:
            return stamp
    return None


def _transcript_overlay(
    row: Optional[Dict[str, Any]],
    status: str,
    anchor: Optional[datetime],
) -> tuple:
    """Override a waiting status with ``working`` when the transcript says so.

    The hooks flip status only on prompt-submit / stop / notification — but
    Claude *resumes* without any of those firing (an answered permission
    prompt or AskUserQuestion, a prompt queued into a running turn), so a
    ``needs-you``/``idle`` stamp sticks while the agent visibly works (#305).
    The transcript JSONL is ground truth: it is appended continuously during
    a turn and goes quiet on stop — and the Stop hook re-stamps the row
    *after* the final transcript write, so a genuine ``needs-you`` still wins
    immediately.

    The probe is two-stage (#309): the mtime ``os.stat`` is only a cheap
    pre-filter — post-Stop metadata lines advance mtime with no real resume —
    so when mtime clears the epsilon, :func:`_last_activity` reads the tail
    and only a real conversation line past the stamp flips the status. Any
    failure keeps the hook status. Returns the (possibly overridden)
    ``(status, age-anchor)``.
    """
    if row is None or status not in ("needs-you", "idle"):
        return status, anchor
    updated = _parse_iso(row.get("updated_at"))
    transcript = row.get("transcript_path")
    if updated is None or not transcript:
        return status, anchor
    try:
        mtime = datetime.fromtimestamp(
            Path(str(transcript)).stat().st_mtime, tz=timezone.utc
        )
    except OSError:
        return status, anchor
    if mtime - updated <= _RESUME_EPSILON:
        return status, anchor  # nothing written past the stamp — skip the read
    activity = _last_activity(transcript)
    if activity is not None and activity - updated > _RESUME_EPSILON:
        return "working", activity
    return status, anchor


def _is_working_ghost(row: Optional[Dict[str, Any]], status: str, now: datetime) -> bool:
    """A state-only ``working`` row whose transcript has gone quiet for far
    longer than any real turn takes is not still working (#322) — it is a
    headless/sdk-cli invocation that finished without ever firing ``Stop`` or
    ``SessionEnd``. Scoped to ``working`` only: a quiet transcript on
    ``needs-you``/``idle`` is the expected, correct shape of a real session
    genuinely waiting on the user, and there is no signal to tell that apart
    from a ghost in those statuses, so they are left untouched.
    """
    if status != "working" or row is None:
        return False
    transcript = row.get("transcript_path")
    if not transcript:
        return False
    try:
        mtime = datetime.fromtimestamp(
            Path(str(transcript)).stat().st_mtime, tz=timezone.utc
        )
    except OSError:
        return False
    return now - mtime > _WORKING_GHOST_AFTER


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
    :func:`_transcript_overlay` (#305).

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
    """Route the three sources into the five computed columns.

    Each column now holds one kind of card (#399): Backlog = open issues.
    Claude's turn = sessions working / unknown / idle (idle is dimmed
    client-side, not hidden — an idle session is still Claude holding a
    workspace). Your turn = needs-you sessions only — a terminal that needs a
    human. Other = everything else that needs attention but isn't a terminal:
    open PRs, then failed/stuck jobs. Done = today's closed issues only — a
    merged PR that closed one is already reflected by the issue itself.
    """
    claude_turn = [c for c in session_cards if c["status"] != "needs-you"]
    your_turn = [c for c in session_cards if c["status"] == "needs-you"]
    return {
        "backlog": list(github.get("issues") or []),
        "claude_turn": claude_turn,
        "your_turn": your_turn,
        "other": list(github.get("prs") or []) + job_cards,
        "done": list(github.get("done") or []),
    }
