"""The last user/assistant exchange of a transcript, for the Board drawer.

Reads only the transcript's tail (:func:`src.board.transcript._tail_lines`)
and degrades to "unavailable" on any IO or parse error.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from src.board.transcript import _EXCHANGE_TAIL_BYTES, _tail_lines


# User lines whose string content is harness plumbing, not a typed prompt:
# slash-command wrappers, local-command output, background-task events.
SKIP_USER_PREFIXES = (
    "<command-", "<local-command-", "<task-notification", "<system-reminder",
)


def typed_user_kind(obj: Dict[str, Any], stripped: str) -> str:
    """Classify an already-stripped, string-content ``user`` line's text:
    ``"injected"`` for an ``isMeta`` line (Claude's own harness-injected
    content — hook output, background-task events, an image's
    ``[Image: ...]`` placeholder — never something a person typed, whatever
    the placeholder text happens to look like), ``"harness"`` for one of
    :data:`SKIP_USER_PREFIXES`'s wrappers (``<command-…>``,
    ``<system-reminder>``…), else ``"typed"``.

    The one classification :func:`last_exchange`, :func:`has_typed_user_prompt`
    (both this module) and ``src.transcript_flavors.claude.claude_entries`` all apply —
    kept here as the single shared check, not reimplemented per caller, so
    every caller agrees on ``isMeta``. Skipping that check lets a
    string-content ``isMeta`` line that no :data:`SKIP_USER_PREFIXES` entry
    catches — an ``[Image: original 1290x2217, …]`` placeholder, a
    ``(Re-invocation of /issue-add — …)`` note — read as a genuine typed
    human prompt in the Board drawer.
    """
    if obj.get("isMeta"):
        return "injected"
    if any(stripped.startswith(p) for p in SKIP_USER_PREFIXES):
        return "harness"
    return "typed"

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


def has_typed_user_prompt(transcript_path: Any) -> Optional[bool]:
    """Whether this conversation ever received a **typed human prompt** —
    ``None`` when that can't be established (#670).

    The discriminator between a real conversation and a bootstrap-only one:
    a session spawned by the launcher and handed a slash command has user
    lines, but every one of them is harness plumbing — the ``<command-…>``
    wrapper, the skill body it expands to, ``<system-reminder>`` blocks, or
    an ``isMeta`` line Claude injects itself. Only a line whose content is a
    plain string, not ``isMeta``, and not one of those wrappers was actually
    typed by a person (or pasted in by the dispatch bar, which is the same
    thing from the transcript's side) — :func:`typed_user_kind`, the same
    classification :func:`last_exchange` already uses to pick the ``user``
    half of an exchange, applied as a yes/no over the tail.

    Three-valued on purpose, because the read is bounded to the last
    :data:`_EXCHANGE_TAIL_BYTES` like every other reader here:

    * ``True`` — a typed prompt is present.
    * ``False`` — the **whole file** fit in the window and held none. This is
      the only confident negative: nothing was ever typed into this
      conversation.
    * ``None`` — unknown. Missing/unreadable path, or a file bigger than the
      window whose tail happened to hold no typed prompt (a long autonomous
      stretch can push the last human turn out of view). Never folded into
      ``False``: "couldn't tell" and "confirmed empty" lead to different
      decisions in :func:`app.webapp.routers.board_chief._find_resumable_chief_session_id`.
    """
    if not transcript_path:
        return None
    lines, truncated = _tail_lines(transcript_path, _EXCHANGE_TAIL_BYTES)
    if not lines:
        return None
    if truncated:
        lines = lines[1:]  # first line is almost certainly a partial record
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "user":
            continue
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        content = msg.get("content")
        if not isinstance(content, str):
            continue  # tool results ride as content lists
        stripped = content.strip()
        if not stripped or typed_user_kind(obj, stripped) != "typed":
            continue
        return True
    return None if truncated else False


def last_exchange(transcript_path: Any) -> Dict[str, Any]:
    """The last completed user→assistant exchange from a transcript JSONL.

    Reads the final :data:`_EXCHANGE_TAIL_BYTES`, walks the lines in reverse:
    the newest assistant line carrying a ``text`` block is the reply (earlier
    lines of the *same* ``message.id`` are prepended — transcripts write one
    line per content block); the nearest preceding user line whose content is
    a plain string is the prompt (list-shaped user content is tool results;
    harness wrappers like ``<command-…>`` and ``isMeta`` lines are skipped —
    see :func:`typed_user_kind`). Missing file, no assistant text in the
    tail → ``{"available": False}`` — never an error.
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
            if not stripped or typed_user_kind(obj, stripped) != "typed":
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
