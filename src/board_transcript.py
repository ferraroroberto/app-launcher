"""Transcript-JSONL parsing for the Board tab (issue #408 split of ``board.py``).

Claude Code's transcript files are the ground truth for two things the hook
state file can't tell you on its own:

* whether a session that's stamped ``needs-you``/``idle`` has actually
  resumed working without any hook firing (:func:`_transcript_overlay`,
  #305/#309), or gone quiet long enough to be a dead headless/sdk-cli ghost
  still stamped ``working`` (:func:`_is_working_ghost`, #322);
* the last completed user→assistant exchange, for the drill-down drawer
  (:func:`last_exchange`, #301).

Both read only a bounded tail of the transcript (never the whole file — a
long session's JSONL can run to many MB) and degrade to "no signal" on any
IO/parse error; callers keep whatever status/state they already had.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from src.board_state import _parse_iso

logger = logging.getLogger(__name__)

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
