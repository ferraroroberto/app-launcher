"""Claude Code hook-JSONL line grammar (#1309, split out of ``session_transcript.py``)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.ask_user_question import TOOL_NAME as ASK_TOOL_NAME, answers_from_result, questions_from_input
from src.board_transcript import _typed_user_kind
from src.plan_review import TOOL_NAME as PLAN_TOOL_NAME, plan_from_input, plan_outcome
from src.transcript_flavors import _shared
from src.transcript_flavors._shared import (
    Entry,
    Line,
    NO_CAP,
    SYSTEM_TEXT_CAP,
    THINKING_TEXT_CAP,
    _attach_result,
    _blocks_text,
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
    apply_patch_result,
)


def _same_path(a: Any, b: Any) -> bool:
    return isinstance(a, str) and isinstance(b, str) and (
        a.replace("\\", "/").lower() == b.replace("\\", "/").lower()
    )


def _patch_from_result(call: Optional[Entry], tool_use_result: Any) -> None:
    """Give an Edit/Write/MultiEdit step Claude's own recorded diff (#1349).

    A successful edit's line carries ``toolUseResult`` with ``filePath``,
    ``structuredPatch`` (hunks with real line numbers) and, for a Write,
    ``type`` — ``"create"`` with an empty patch and the new file under
    ``content``. The shape was probed from the 40 newest transcripts on the
    dev box (key names and types only). Only applied when the line holds this
    one result and names the call's own file; a failed call records a plain
    error string instead and keeps its action untouched.
    """
    action = call.get("action") if call is not None else None
    if not isinstance(action, dict) or action.get("verb") not in ("edited", "wrote"):
        return
    if not isinstance(tool_use_result, dict) or not _same_path(tool_use_result.get("filePath"), action.get("path")):
        return
    created = tool_use_result.get("content") if tool_use_result.get("type") == "create" else None
    apply_patch_result(action, tool_use_result.get("structuredPatch"), created)


def claude_entries(lines: List[Line], *, uncapped: bool = False) -> List[Entry]:
    """Typed entries from Claude Code hook-JSONL lines, in file order.

    One transcript line is one content block; assistant ``text`` blocks of
    the same ``message.id`` merge into one ``assistant`` entry (the same rule
    :func:`board_transcript.last_exchange` applies). A ``user`` line is a
    typed prompt when it is a plain string outside
    :data:`board_transcript._SKIP_USER_PREFIXES` and not ``isMeta`` — or a
    list of ``text``/``image`` blocks (an image-paste prompt) with no tool
    result. Tool results pair with their call by ``tool_use_id``. Lines on a
    sidechain (a sub-agent's traffic) keep their kind but carry
    ``sidechain: True`` so the client folds them and pagination doesn't count
    them as turns. Metadata rows (``mode``, ``ai-title``, ``attachment``, …)
    carry no conversation text and are dropped.

    ``uncapped`` (#985) drops the ``assistant``/``user`` display caps for
    :func:`session_transcript.entry_full_text` — the folded kinds keep their
    normal caps either way, since only a turn's own text is ever read back
    from an uncapped call.
    """
    assistant_cap = NO_CAP if uncapped else _shared.ASSISTANT_TEXT_CAP
    user_cap = NO_CAP if uncapped else _shared.USER_TEXT_CAP
    entries: List[Entry] = []
    open_calls: Dict[str, Entry] = {}
    assistant_by_mid: Dict[str, Entry] = {}
    for offset, raw in lines:
        obj = _loads(raw)
        if obj is None:
            continue
        kind = obj.get("type")
        ts = obj.get("timestamp")
        sidechain = bool(obj.get("isSidechain"))
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        content = msg.get("content")

        if kind == "assistant":
            if not isinstance(content, list):
                continue
            mid = str(msg.get("id") or "")
            for block in content:
                if not isinstance(block, dict):
                    continue
                bt = block.get("type")
                if bt == "text":
                    text = str(block.get("text") or "").strip()
                    if not text:
                        continue
                    prev = assistant_by_mid.get(mid) if mid else None
                    if prev is not None:
                        joined, truncated = _cap(prev["text"] + "\n\n" + text, assistant_cap)
                        prev["text"] = joined
                        prev["truncated"] = prev["truncated"] or truncated
                        continue
                    e = _text_entry("assistant", offset, ts, text, assistant_cap, sidechain=sidechain)
                    entries.append(e)
                    if mid:
                        assistant_by_mid[mid] = e
                elif bt == "thinking":
                    text = str(block.get("thinking") or "").strip()
                    if text:
                        entries.append(_text_entry("thinking", offset, ts, text, THINKING_TEXT_CAP, sidechain=sidechain))
                elif bt == "tool_use":
                    e = _with_action(_entry(
                        "tool_call", offset, ts,
                        name=str(block.get("name") or "tool"),
                        summary=_tool_summary(block.get("input")),
                        result=None, result_truncated=False, sidechain=sidechain,
                    ), _tool_action(str(block.get("name") or ""), block.get("input")))
                    if e["name"] == ASK_TOOL_NAME:
                        # The Chat pane's question card (#1149): the one tool
                        # whose structured input is forwarded, plus the id
                        # the answer route checks against. An input this
                        # reader doesn't recognise keeps the generic row.
                        questions = questions_from_input(block.get("input"))
                        if questions:
                            e["questions"] = questions
                            e["call_id"] = str(block.get("id") or "")
                    elif e["name"] == PLAN_TOOL_NAME:
                        # The plan card (#1151): the whole plan, not the
                        # summary's one truncated line of it.
                        plan = plan_from_input(block.get("input"))
                        if plan:
                            e["plan"], e["plan_truncated"] = plan
                            e["call_id"] = str(block.get("id") or "")
                    entries.append(e)
                    if block.get("id"):
                        open_calls[str(block["id"])] = e
            continue

        if kind == "user":
            if isinstance(content, list):
                results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
                if results:
                    tool_use_result = obj.get("toolUseResult")
                    # Image numbering walks the whole line (#1265), so each
                    # result's images start after the ones before it.
                    image_n = 0
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "image":
                            image_n += 1
                            continue
                        if block.get("type") != "tool_result":
                            continue
                        call = open_calls.get(str(block.get("tool_use_id")))
                        result_images = _line_image_blocks([block])
                        refs, missing = _image_refs(result_images, offset, image_n)
                        image_n += len(result_images)
                        result_text = _with_placeholders(_blocks_text(block.get("content")), missing)
                        # A decision card's answer rides on the *line*
                        # (`toolUseResult`) or in the rejection text — #1149's
                        # picks, #1151's plan outcome; empty for other tools.
                        decision: Dict[str, Any] = {}
                        answers = answers_from_result(tool_use_result)
                        if answers:
                            decision["answers"] = answers
                        decision.update(plan_outcome(
                            call.get("name") if call else None, tool_use_result,
                            result_text, bool(block.get("is_error")),
                        ) or {})
                        _attach_result(
                            entries, open_calls, block.get("tool_use_id"),
                            result_text, offset, ts, sidechain,
                            # Claude states it outright: across the 40 newest
                            # transcripts on the dev box a `tool_result` block
                            # came in exactly two key-sets, one of them carrying
                            # `is_error` beside the error text (#1020).
                            error=bool(block.get("is_error")),
                            decision=decision,
                            images=refs,
                        )
                        if len(results) == 1 and not block.get("is_error"):
                            _patch_from_result(call, tool_use_result)
                    continue
                refs, missing = _image_refs(_line_image_blocks(content), offset, 0)
                text = _with_placeholders(_blocks_text(content), missing)
            elif isinstance(content, str):
                text, refs = content, []
            else:
                continue
            stripped = text.strip()
            if not stripped and not refs:
                continue
            # `_typed_user_kind` (board_transcript.py, #1310) is the one
            # classification shared with `last_exchange`/`has_typed_user_prompt`
            # so the Board drawer's "typed prompt" reading never drifts from
            # this reader's own.
            user_kind = _typed_user_kind(obj, stripped)
            if user_kind == "injected":
                entries.append(_text_entry("system", offset, ts, stripped, SYSTEM_TEXT_CAP,
                                           label="injected", sidechain=sidechain))
            elif user_kind == "harness":
                entries.append(_text_entry("system", offset, ts, stripped, SYSTEM_TEXT_CAP,
                                           label=_harness_label(stripped), sidechain=sidechain))
            else:
                user = _text_entry("user", offset, ts, stripped, user_cap, sidechain=sidechain)
                if refs:
                    user["images"] = refs
                entries.append(user)
            continue

        if kind == "system":
            text = str(obj.get("content") or "").strip()
            if text:
                entries.append(_text_entry("system", offset, ts, text, SYSTEM_TEXT_CAP,
                                           label=str(obj.get("subtype") or "system"), sidechain=sidechain))
            continue
        # Everything else is metadata without conversation text.
    return entries


def _claude_line_key(raw: str) -> Optional[str]:
    """The ``message.id`` of an assistant line — the key that groups one
    message's one-line-per-block records — else None (self-contained line)."""
    obj = _loads(raw)
    if obj is None or obj.get("type") != "assistant":
        return None
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    mid = msg.get("id")
    return str(mid) if mid else None
