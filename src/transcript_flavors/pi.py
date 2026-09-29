"""Pi session-history JSONL line grammar (#1309, split out of ``session_transcript.py``)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from src.board_transcript import _SKIP_USER_PREFIXES
from src.transcript_flavors import _shared
from src.transcript_flavors._shared import (
    Entry,
    Line,
    NO_CAP,
    SYSTEM_TEXT_CAP,
    THINKING_TEXT_CAP,
    _attach_result,
    _cap,
    _entry,
    _harness_label,
    _image_refs,
    _line_image_blocks,
    _loads,
    _text_entry,
    _tool_action,
    _tool_summary,
    _with_action,
    _with_placeholders,
)

# Record types Pi writes that carry no conversation text. ``custom`` is the
# one that matters: every ``claude-agent-sdk-tool-watch`` record restates a
# tool execution the ``toolResult`` message already carries (228 of them in
# the on-disk corpus, all that one ``customType``), so reading them would
# show every tool result twice. ``session``/``session_info`` are the file
# header and a rename.
_PI_DROP_TYPES = frozenset({"session", "session_info", "custom"})

# Pi user messages that are harness plumbing rather than something typed:
# its skill loader injects a whole SKILL.md as a user turn. Joined with the
# Claude-side prefixes, which Pi's own harness emits too.
_PI_SKIP_USER_PREFIXES = _SKIP_USER_PREFIXES + ("<skill name=",)

# Settings records, folded into `system` so the model a session actually ran
# on is visible without unfolding a wall of plumbing.
_PI_SETTING_KINDS = {
    "model_change": ("model", ("provider", "modelId")),
    "thinking_level_change": ("thinking", ("thinkingLevel",)),
}


def _pi_blocks(content: Any) -> Tuple[str, int]:
    """``(joined text, image count)`` for one Pi message's content list.

    An ``image`` block carries raw base64 in ``data`` — never text — so it
    is counted, not decoded, and the caller renders a placeholder the way
    :func:`src.transcript_flavors.claude.claude_entries` does for an
    image-paste prompt.
    """
    if not isinstance(content, list):
        return "", 0
    parts: List[str] = []
    images = 0
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = str(block.get("text") or "").strip()
            if text:
                parts.append(text)
        elif block.get("type") == "image":
            images += 1
    return "\n\n".join(parts), images


def pi_entries(lines: List[Line], *, uncapped: bool = False) -> List[Entry]:
    """Typed entries from a Pi session's history JSONL, in file order.

    Pi writes one record per line and — unlike Claude — puts a message's
    **whole content list on that one line**, so a single line can produce a
    ``thinking``, an ``assistant`` and several ``tool_call`` entries, all
    sharing its byte offset. Measured on the 57 sessions on disk plus a
    probe taken for #1013:

    * A tool call is an assistant content block (``{"type": "toolCall",
      "id", "name", "arguments"}``), not a record of its own; its result is
      a separate message with ``role: "toolResult"``, paired by
      ``toolCallId`` (which may itself contain a ``|``).
    * Message ids are unique per line — no message ever spans two lines
      (0 duplicates across the corpus) — so :func:`_pi_line_key` returns
      ``None`` and nothing here needs a neighbouring line to be read.
    * A message's ``text`` blocks are never separated by a ``toolCall``
      (0 of 1490 assistant messages), so merging them into one
      ``assistant`` entry cannot hoist a reply above the tool call that
      produced it — the trap the Grok flavour had to avoid (#1012). It also
      keeps exactly one turn entry per offset, which is what
      :func:`session_transcript.entry_full_text` resolves against.

    Timestamps come from the **outer** record's ISO string, not the inner
    ``message.timestamp`` (epoch milliseconds), so a turn renders as itself
    rather than as 1970.

    Known omission, shared with the Codex and Grok flavours: a failed tool
    result (``isError: true``) reads the same as a successful one, because
    the ``Entry`` contract has no error field and the client renders none.

    ``uncapped`` — see :func:`src.transcript_flavors.claude.claude_entries`.
    """
    assistant_cap = NO_CAP if uncapped else _shared.ASSISTANT_TEXT_CAP
    user_cap = NO_CAP if uncapped else _shared.USER_TEXT_CAP
    entries: List[Entry] = []
    open_calls: Dict[str, Entry] = {}
    for offset, raw in lines:
        obj = _loads(raw)
        if obj is None:
            continue
        kind = obj.get("type")
        if kind in _PI_DROP_TYPES:
            continue
        ts = obj.get("timestamp")

        if kind in _PI_SETTING_KINDS:
            label, fields = _PI_SETTING_KINDS[kind]
            text = " ".join(str(obj.get(f) or "") for f in fields).strip()
            if text:
                entries.append(_text_entry("system", offset, ts, text, SYSTEM_TEXT_CAP,
                                           label=label, sidechain=False))
            continue

        if kind == "compaction":
            # The history was summarised and rewound here; without this the
            # conversation would just appear to jump.
            text = str(obj.get("summary") or "").strip()
            if text:
                entries.append(_text_entry("system", offset, ts, text, SYSTEM_TEXT_CAP,
                                           label="compaction", sidechain=False))
            continue

        if kind != "message":
            continue
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        role = str(msg.get("role") or "")
        content = msg.get("content")

        if role == "assistant":
            if not isinstance(content, list):
                continue
            reply: Optional[Entry] = None
            for block in content:
                if not isinstance(block, dict):
                    continue
                bt = block.get("type")
                if bt == "text":
                    text = str(block.get("text") or "").strip()
                    if not text:
                        continue
                    if reply is not None:
                        joined, truncated = _cap(reply["text"] + "\n\n" + text, assistant_cap)
                        reply["text"] = joined
                        reply["truncated"] = reply["truncated"] or truncated
                        continue
                    reply = _text_entry("assistant", offset, ts, text, assistant_cap,
                                        sidechain=False)
                    entries.append(reply)
                elif bt == "thinking":
                    # `thinkingSignature` rides alongside and is an encrypted
                    # blob many times the size of the thought — never shown.
                    text = str(block.get("thinking") or "").strip()
                    if text:
                        entries.append(_text_entry("thinking", offset, ts, text,
                                                   THINKING_TEXT_CAP, sidechain=False))
                elif bt == "toolCall":
                    call = _with_action(_entry(
                        "tool_call", offset, ts,
                        name=str(block.get("name") or "tool"),
                        summary=_tool_summary(block.get("arguments")),
                        result=None, result_truncated=False, sidechain=False,
                    ), _tool_action(str(block.get("name") or ""), block.get("arguments")))
                    entries.append(call)
                    if block.get("id"):
                        open_calls[str(block["id"])] = call
            continue

        if role == "toolResult":
            text, _count = _pi_blocks(content)
            refs, missing = _image_refs(_line_image_blocks(content), offset, 0)
            text = _with_placeholders(text, missing)
            # Pi writes `isError` on every `toolResult`, both polarities
            # (`tests/fixtures/pi_session.jsonl` carries one of each) — #1020.
            _attach_result(entries, open_calls, msg.get("toolCallId"), text,
                           offset, ts, False, error=bool(msg.get("isError")), images=refs)
            continue

        if role == "user":
            text, _count = _pi_blocks(content)
            refs, missing = _image_refs(_line_image_blocks(content), offset, 0)
            text = _with_placeholders(text, missing)
            stripped = text.strip()
            if not stripped and not refs:
                continue
            if any(stripped.startswith(p) for p in _PI_SKIP_USER_PREFIXES):
                label = "skill" if stripped.startswith("<skill name=") else _harness_label(stripped)
                entries.append(_text_entry("system", offset, ts, stripped, SYSTEM_TEXT_CAP,
                                           label=label, sidechain=False))
            else:
                user = _text_entry("user", offset, ts, stripped, user_cap, sidechain=False)
                if refs:
                    user["images"] = refs
                entries.append(user)
    return entries


def _pi_line_key(raw: str) -> Optional[str]:
    """Pi records are self-contained: one line carries a message's whole
    content list (see :func:`pi_entries`), so no line ever needs another."""
    return None
