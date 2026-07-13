"""Transcript-JSONL parsing for the Board tab (issue #408 split of ``board.py``).

Claude Code's transcript files are the ground truth for three things the hook
state file can't tell you on its own:

* whether a session that's stamped ``needs-you``/``idle`` has actually
  resumed working without any hook firing (:func:`_transcript_overlay`,
  #305/#309), and whether an unmatched hook row has recent transcript
  activity that independently proves an external process still exists
  (:func:`_external_row_liveness`, #322/#455);
* whether a session stamped ``needs-you``/``idle`` is actually still waiting
  on its *own* backgrounded work — a background sub-agent or shell dispatch
  it hasn't heard back from yet (:func:`_has_pending_background_dispatch`,
  #464);
* the last completed user→assistant exchange, for the drill-down drawer
  (:func:`last_exchange`, #301).

All read only a bounded tail of the transcript (never the whole file — a
long session's JSONL can run to many MB) and degrade to "no signal" on any
IO/parse error; callers keep whatever status/state they already had.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.board_state import _parse_iso

logger = logging.getLogger(__name__)

# Transcript-activity overlay (#305): a transcript appended this much later
# than the row's stamp means Claude resumed without any hook firing. The
# margin absorbs the Stop hook and the final transcript write landing a
# couple of seconds apart in either order.
_RESUME_EPSILON = timedelta(seconds=10)

# External-row liveness (#322, tightened by #455): a hook state is semantic
# evidence, not proof that its process still exists. Only recent transcript
# activity lets an unmatched row render as an external session. Missing cloud
# / bridge transcripts and quiet waiting rows otherwise linger for 24 hours.
_EXTERNAL_ACTIVITY_AFTER = timedelta(minutes=15)

_ACTIVITY_TAIL_BYTES = 8 * 1024

# Pending-background-dispatch detection (#464): the id a completed dispatch
# is later referenced by, e.g. "<task-id>btvos2agp</task-id>".
_TASK_ID_RE = re.compile(r"<task-id>([^<]+)</task-id>")


def _tail_lines(path: Any, n_bytes: int) -> Tuple[List[str], bool]:
    """Read the last ``n_bytes`` of ``path``, decoded and split into lines.

    Also returns whether the read was truncated (the file is bigger than
    ``n_bytes`` — the first returned line is then likely a torn partial
    record, which callers that care skip). Best-effort: any OSError (missing
    file, read failure) returns ``([], False)`` — callers degrade to "no
    signal" the same way a parse failure would. Shared by
    :func:`_last_activity` and :func:`last_exchange`, which differ only in
    the byte window and what they do with the lines.
    """
    try:
        with Path(str(path)).open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - n_bytes))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
            return lines, size > n_bytes
    except OSError:
        return [], False


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
    lines, _truncated = _tail_lines(transcript_path, _ACTIVITY_TAIL_BYTES)
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


def _transcript_mtime(transcript_path: Any) -> Optional[datetime]:
    """The transcript file's mtime as a UTC datetime, or ``None`` on any
    OSError (missing file, permission, …). Shared by
    :func:`_transcript_overlay` and :func:`_external_row_liveness` — both need
    this same cheap stat probe before deciding whether a costlier tail read
    (or, for the ghost check, a status flip) is warranted.
    """
    try:
        return datetime.fromtimestamp(
            Path(str(transcript_path)).stat().st_mtime, tz=timezone.utc
        )
    except OSError:
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
    and only a real conversation line past the stamp flips the status.

    Separately (#464), :func:`_has_pending_background_dispatch` always checks
    the tail for an outstanding background sub-agent/shell dispatch launched
    *before* Claude's own turn ended — a case the mtime pre-filter above
    would otherwise skip, since nothing is written to this transcript past
    the stamp until the background work's own completion notice lands.

    Any failure keeps the hook status. Returns the (possibly overridden)
    ``(status, age-anchor)``.
    """
    if row is None or status not in ("needs-you", "idle"):
        return status, anchor
    updated = _parse_iso(row.get("updated_at"))
    transcript = row.get("transcript_path")
    if updated is None or not transcript:
        return status, anchor
    mtime = _transcript_mtime(transcript)
    if mtime is None:
        return status, anchor
    if mtime - updated > _RESUME_EPSILON:
        activity = _last_activity(transcript)
        if activity is not None and activity - updated > _RESUME_EPSILON:
            return "working", activity
    if _has_pending_background_dispatch(transcript):
        return "working", anchor
    return status, anchor


def _launched_background_ids(obj: Dict[str, Any]) -> List[str]:
    """Background-dispatch ids a transcript line's tool result evidences.

    The synchronous ack for a backgrounded dispatch rides ``toolUseResult``
    (a sibling of ``message``, not inside it, per live transcripts): a
    backgrounded ``Bash`` command carries ``backgroundTaskId``; an async
    sub-agent dispatch (the ``Agent``/``Task`` tool) carries ``isAsync: true``
    plus ``agentId``. Both ids later reappear inside a completion's
    ``<task-id>`` (see :func:`_notified_background_ids`).
    """
    result = obj.get("toolUseResult")
    if not isinstance(result, dict):
        return []
    ids: List[str] = []
    background_id = result.get("backgroundTaskId")
    if isinstance(background_id, str) and background_id:
        ids.append(background_id)
    if result.get("isAsync"):
        agent_id = result.get("agentId")
        if isinstance(agent_id, str) and agent_id:
            ids.append(agent_id)
    return ids


def _notified_background_ids(obj: Dict[str, Any]) -> List[str]:
    """Background-dispatch ids a ``<task-notification>`` line marks complete.

    Claude Code injects the completion notice as a ``queue-operation`` line's
    plain ``content`` string, or an ``attachment`` line's
    ``attachment.prompt`` — never as an ordinary ``assistant``/``user``
    message, so it is invisible to :func:`_last_activity` (empirically
    confirmed against live transcripts, app-launcher#464). Either shape
    carries the same ``<task-id>ID</task-id>``.
    """
    text = obj.get("content")
    if not isinstance(text, str) or "<task-notification>" not in text:
        attachment = obj.get("attachment")
        text = attachment.get("prompt") if isinstance(attachment, dict) else None
    if not isinstance(text, str) or "<task-notification>" not in text:
        return []
    return _TASK_ID_RE.findall(text)


def _has_pending_background_dispatch(transcript_path: Any) -> bool:
    """Whether the tail shows a background dispatch with no completion yet.

    A ``Stop`` hook fires (stamping ``needs-you``) the moment Claude's own
    turn ends — even when that turn dispatched a background sub-agent or
    shell command it is still waiting to hear back from. The parent
    transcript then goes quiet until the eventual ``<task-notification>``
    lands, so #305's activity check alone never catches this window (#464).
    Unlike :func:`_last_activity` this runs unconditionally, not mtime-gated:
    the dispatch's launch line sits *before* the hook's stamp, not after it,
    so the mtime pre-filter that skips a quiet tail would otherwise hide it.
    Any failure degrades to "nothing pending" — callers keep the hook status.
    """
    lines, _truncated = _tail_lines(transcript_path, _ACTIVITY_TAIL_BYTES)
    launched: set = set()
    completed: set = set()
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        launched.update(_launched_background_ids(obj))
        completed.update(_notified_background_ids(obj))
    return bool(launched - completed)


def _external_row_liveness(
    row: Optional[Dict[str, Any]], now: datetime
) -> Tuple[bool, str]:
    """Whether an unmatched hook row has independent process-liveness proof.

    A hook row can survive a hard kill or a cloud/bridge lifecycle gap for the
    writer's whole 24-hour retention window. Its status says what the agent was
    doing at the last event; it does not say the process is still alive. For a
    row with no launcher-owned session-host match, only a transcript written in
    the last 15 minutes is enough evidence to render an external card.

    The reason string is deliberately condition-specific so the caller can
    leave one useful info-level breadcrumb for the next recurrence (#455).
    """
    if row is None:
        return False, "missing state row"
    transcript = row.get("transcript_path")
    if not transcript:
        return False, "missing transcript path"
    mtime = _transcript_mtime(transcript)
    if mtime is None:
        return False, "transcript file unavailable"
    if now - mtime > _EXTERNAL_ACTIVITY_AFTER:
        return False, "transcript quiet past 15 minutes"
    return True, "recent transcript activity"


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
    lines, truncated = _tail_lines(transcript_path, _EXCHANGE_TAIL_BYTES)
    if truncated and lines:
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
