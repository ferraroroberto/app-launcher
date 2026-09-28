"""Codex rollout ``response_item`` line grammar (#1309, split out of ``session_transcript.py``)."""

from __future__ import annotations

from typing import Dict, List, Optional

from src.transcript_flavors import _shared
from src.transcript_flavors._shared import (
    Entry,
    Line,
    NO_CAP,
    SYSTEM_TEXT_CAP,
    THINKING_TEXT_CAP,
    _attach_result,
    _blocks_text,
    _entry,
    _loads,
    _text_entry,
    _tool_action,
    _tool_summary,
    _with_action,
)

# Codex user-role messages that are harness plumbing, not a typed prompt —
# the Codex counterpart of `board_transcript._SKIP_USER_PREFIXES`.
_CODEX_SKIP_USER_PREFIXES = (
    "<environment_context>", "<user_instructions>", "<recommended_plugins>",
    "<skills_instructions>", "<turn_aborted>", "<permissions_instructions>",
    "<collaboration_mode>",
)


def codex_entries(lines: List[Line], *, uncapped: bool = False) -> List[Entry]:
    """Typed entries from a Codex rollout's ``response_item`` lines.

    ``message`` payloads carry ``input_text`` (user/developer) or
    ``output_text`` (assistant) blocks — developer-role and harness-tagged
    user messages are ``system``. ``function_call`` / ``custom_tool_call``
    pair with their ``*_output`` by ``call_id``; ``reasoning`` summaries
    become ``thinking``; ``agent_message`` (sub-agent mailbox traffic) is a
    sidechain ``system`` entry. Everything else (token usage, events, world
    state) is dropped.

    ``uncapped`` — see :func:`src.transcript_flavors.claude.claude_entries`.
    """
    assistant_cap = NO_CAP if uncapped else _shared.ASSISTANT_TEXT_CAP
    user_cap = NO_CAP if uncapped else _shared.USER_TEXT_CAP
    entries: List[Entry] = []
    open_calls: Dict[str, Entry] = {}
    for offset, raw in lines:
        obj = _loads(raw)
        if obj is None or obj.get("type") != "response_item":
            continue
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        ts = obj.get("timestamp")
        pt = payload.get("type")
        if pt == "message":
            role = str(payload.get("role") or "")
            if role == "assistant":
                text = _blocks_text(payload.get("content"), types=("output_text",))
                if text.strip():
                    entries.append(_text_entry("assistant", offset, ts, text, assistant_cap, sidechain=False))
            elif role in ("user", "developer"):
                text = _blocks_text(payload.get("content"), types=("input_text",))
                stripped = text.strip()
                if not stripped:
                    continue
                if role == "developer" or any(stripped.startswith(p) for p in _CODEX_SKIP_USER_PREFIXES):
                    entries.append(_text_entry("system", offset, ts, stripped, SYSTEM_TEXT_CAP,
                                               label="developer" if role == "developer" else "system",
                                               sidechain=False))
                else:
                    entries.append(_text_entry("user", offset, ts, stripped, user_cap, sidechain=False))
        elif pt in ("function_call", "custom_tool_call"):
            e = _with_action(_entry(
                "tool_call", offset, ts,
                name=str(payload.get("name") or "tool"),
                summary=_tool_summary(payload.get("arguments") if pt == "function_call" else payload.get("input")),
                result=None, result_truncated=False, sidechain=False,
            ), _tool_action(str(payload.get("name") or ""), payload.get("input"), raw_text=True)
                if pt == "custom_tool_call" else None)
            entries.append(e)
            if payload.get("call_id"):
                open_calls[str(payload["call_id"])] = e
        elif pt in ("function_call_output", "custom_tool_call_output"):
            # No `error=` here, deliberately (#1020): Codex records **no**
            # success/error field at any level. A failed call and a working
            # one are structurally identical — measured over the 30 newest
            # rollouts (one key-set, `call_id,id,
            # internal_chat_message_metadata_passthrough,output,type`) and
            # re-confirmed by probe, where a failing tool wrote "Script
            # failed" and a working one "Script completed" in the same
            # `output` text and nothing else differed. Telling them apart
            # would mean reading that prose; this flavour is declared
            # `none` in `FLAVORS` instead, so the client says "can't tell"
            # rather than showing a failure as a success.
            output = payload.get("output")
            text = output if isinstance(output, str) else _blocks_text(output, types=("input_text", "output_text", "text"))
            _attach_result(entries, open_calls, payload.get("call_id"), text, offset, ts, False)
        elif pt == "reasoning":
            text = _blocks_text(payload.get("summary"), types=("summary_text",))
            if text.strip():
                entries.append(_text_entry("thinking", offset, ts, text, THINKING_TEXT_CAP, sidechain=False))
        elif pt == "agent_message":
            text = _blocks_text(payload.get("content"), types=("input_text", "output_text"))
            if text.strip():
                entries.append(_text_entry("system", offset, ts, text, SYSTEM_TEXT_CAP,
                                           label="sub-agent", sidechain=True))
    return entries


def _codex_line_key(raw: str) -> Optional[str]:
    """Codex rollout records are self-contained: never grouped."""
    return None
