"""Grok Build ACP-style ``updates.jsonl`` line grammar (#1309, split out of ``session_transcript.py``)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.transcript_flavors import _shared
from src.transcript_flavors._shared import (
    Entry,
    Line,
    NO_CAP,
    THINKING_TEXT_CAP,
    _attach_result,
    _cap,
    _entry,
    _loads,
    _text_entry,
    _tool_action,
    _tool_summary,
    _with_action,
)

# ``sessionUpdate`` kinds that carry conversation text, and the entry kind
# each becomes. Everything else Grok writes (``turn_completed``,
# ``hook_execution``, and the metadata half of a ``tool_call_update``) is
# bookkeeping without text.
_GROK_CHUNK_KINDS = {
    "user_message_chunk": "user",
    "agent_message_chunk": "assistant",
    "agent_thought_chunk": "thinking",
}


def _grok_timestamp(obj: Dict[str, Any], meta: Dict[str, Any]) -> Optional[str]:
    """Grok's epoch stamp as the ISO-8601 string the other flavours emit.

    Grok writes ``timestamp`` in epoch *seconds* and ``_meta
    .agentTimestampMs`` in milliseconds; the client renders a turn's stamp
    with ``new Date(ts)``, which reads a bare number as milliseconds — an
    epoch-seconds value would render as 1970. Converting here keeps the
    ``Entry`` contract one shape for every flavour instead of teaching the
    client a per-agent format.
    """
    raw_ms = meta.get("agentTimestampMs")
    seconds: Optional[float] = None
    if isinstance(raw_ms, (int, float)) and not isinstance(raw_ms, bool):
        seconds = float(raw_ms) / 1000.0
    else:
        raw_s = obj.get("timestamp")
        if isinstance(raw_s, (int, float)) and not isinstance(raw_s, bool):
            seconds = float(raw_s)
    if seconds is None:
        return None
    try:
        stamp = datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return stamp.isoformat().replace("+00:00", "Z")


# The `tool_call_update` statuses that carry a *result* (#1020). `failed`
# was not in the on-disk corpus when the Grok reader was written — 19
# records, every one `completed` — so it was excluded and a failed call's
# result was dropped entirely, rendering as "no result on this page" rather
# than as a failure. A probe settled it: `grok -p` reading a missing path
# writes `status: "failed"` with the same key-set as a completed one
# (`content,rawOutput,sessionUpdate,status,toolCallId`), the error text in
# both. ACP's other two statuses (`pending`, `in_progress`) are progress,
# not results, and stay excluded — attaching one would occupy the call's
# single result slot and lock the real one out.
_GROK_RESULT_STATUSES = frozenset({"completed", "failed"})


def _grok_result_text(update: Dict[str, Any]) -> str:
    """The text of a result-carrying ``tool_call_update``.

    Same for a ``completed`` and a ``failed`` one: the probe on #1020
    showed a failure carrying its message in the very same ``content``
    blocks, so nothing here branches on the status.

    Two sources, in order. ACP's own ``content`` blocks
    (``{"type": "content", "content": {"type": "text", ...}}``) are the
    portable one, but a measured capture showed them absent for some tools
    (``ListDir``, ``SchedulerList`` had none; ``ReadFile`` and
    ``GrepSearch`` had them), so the fallback reaches one level into the
    tool-specific ``rawOutput`` union for a nested string ``content``
    (``ListDir``'s ``Content.content``, ``ReadFile``'s
    ``FileContent.content``). Anything else — notably ``GrepSearch``'s
    ``stdout``, which is a JSON *array of byte values* — is reported as its
    output type rather than decoded or dumped, so a textless result reads
    as itself instead of as an empty one.
    """
    blocks = update.get("content")
    if isinstance(blocks, list):
        parts = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            inner = block.get("content")
            if isinstance(inner, dict) and inner.get("type") == "text":
                text = str(inner.get("text") or "").strip()
                if text:
                    parts.append(text)
        if parts:
            return "\n\n".join(parts)
    raw = update.get("rawOutput")
    if isinstance(raw, dict):
        for value in raw.values():
            if isinstance(value, dict):
                text = value.get("content")
                if isinstance(text, str) and text.strip():
                    return text
        kind = raw.get("type")
        if kind:
            return f"({kind}: no text output)"
    return ""


def grok_entries(lines: List[Line], *, uncapped: bool = False) -> List[Entry]:
    """Typed entries from a Grok Build ``updates.jsonl`` stream, in file order.

    Grok appends one ACP-style ``session/update`` record per line.
    ``user_message_chunk`` / ``agent_message_chunk`` / ``agent_thought_chunk``
    carry the conversation text (``user`` / ``assistant`` / ``thinking``);
    ``tool_call`` opens a call and the ``status: "completed"`` half of a
    ``tool_call_update`` closes it, paired by ``toolCallId``.

    **Each ``*_chunk`` line is already a whole prose segment**, not a
    stream fragment: Grok folds the stream itself before writing (a
    measured line carrying a full three-paragraph, 2.6 KB reply reported
    ``chunkId: 627``). Several ``agent_message_chunk`` lines in one turn are
    therefore *separate* segments either side of a tool call — a preamble
    and a final answer — and merging them by turn would hoist the answer
    above the tool call that produced it. So only *consecutive* same-kind
    chunks merge (keeping the first one's offset, per the cursor contract),
    which in every capture taken was a no-op and stays a safety net if Grok
    ever writes a segment in pieces.

    ``uncapped`` — see :func:`src.transcript_flavors.claude.claude_entries`.
    """
    assistant_cap = NO_CAP if uncapped else _shared.ASSISTANT_TEXT_CAP
    user_cap = NO_CAP if uncapped else _shared.USER_TEXT_CAP
    caps = {"user": user_cap, "assistant": assistant_cap, "thinking": THINKING_TEXT_CAP}
    entries: List[Entry] = []
    open_calls: Dict[str, Entry] = {}
    # The entry the previous line produced, when it was a chunk — the only
    # thing a following chunk of the same kind may merge into.
    last_chunk: Optional[Entry] = None
    for offset, raw in lines:
        obj = _loads(raw)
        if obj is None:
            continue
        params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
        update = params.get("update") if isinstance(params.get("update"), dict) else {}
        kind = update.get("sessionUpdate")
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        ts = _grok_timestamp(obj, meta)

        if kind in _GROK_CHUNK_KINDS:
            entry_kind = _GROK_CHUNK_KINDS[kind]
            content = update.get("content")
            text = ""
            if isinstance(content, dict) and content.get("type") == "text":
                text = str(content.get("text") or "")
            text = text.strip()
            if not text:
                continue
            cap = caps[entry_kind]
            if last_chunk is not None and last_chunk["kind"] == entry_kind:
                joined, truncated = _cap(last_chunk["text"] + "\n\n" + text, cap)
                last_chunk["text"] = joined
                last_chunk["truncated"] = last_chunk["truncated"] or truncated
                continue
            entry = _text_entry(entry_kind, offset, ts, text, cap, sidechain=False)
            entries.append(entry)
            last_chunk = entry
            continue

        last_chunk = None

        if kind == "tool_call":
            entry = _with_action(_entry(
                "tool_call", offset, ts,
                name=str(update.get("title") or "tool"),
                summary=_tool_summary(update.get("rawInput")),
                result=None, result_truncated=False, sidechain=False,
            ), _tool_action(str(update.get("title") or ""), update.get("rawInput")))
            entries.append(entry)
            if update.get("toolCallId"):
                open_calls[str(update["toolCallId"])] = entry
        elif kind == "tool_call_update" and update.get("status") in _GROK_RESULT_STATUSES:
            # The other half of a `tool_call_update` (no `status`, carrying
            # `locations`/`kind`) only restates the call, so it is dropped.
            _attach_result(
                entries, open_calls, update.get("toolCallId"),
                _grok_result_text(update), offset, ts, False,
                error=update.get("status") == "failed",
            )
        # `turn_completed` and `hook_execution` carry no conversation text.
    return entries


def _grok_line_key(raw: str) -> Optional[str]:
    """Grok records are self-contained: one line is one whole segment
    (see :func:`grok_entries`), so no line ever needs another to be read."""
    return None
