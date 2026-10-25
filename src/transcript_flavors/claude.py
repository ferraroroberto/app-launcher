"""Claude Code hook-JSONL line grammar (#1309, split out of ``session_transcript.py``)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from src.ask_user_question import TOOL_NAME as ASK_TOOL_NAME, answers_from_result, questions_from_input
from src.board.transcript_exchange import _typed_user_kind
from src.plan_review import TOOL_NAME as PLAN_TOOL_NAME, plan_from_input, plan_outcome
from src.session_view.outbox import clean_name, kind_of
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
    _tool_description,
    _tool_summary,
    _with_action,
    _with_placeholders,
    DIFF_FULL_BYTES,
    Hunk,
    cap_diff,
    diff_counts,
    recorded_hunks,
)


# Claude Code's own "put this file in front of the owner" tool. Its
# input is ``{"files": [path, ...], "caption"?: str}``, paths absolute or
# relative to the session's cwd; the Chat pane renders each one the way an
# outbox file renders, through ``/transcript/file`` and the outbox's guards.
SEND_FILE_TOOL = "SendUserFile"


def sent_file_paths(inputs: Any) -> List[str]:
    """The paths a ``SendUserFile`` input lists, in order; [] for any other shape."""
    files = inputs.get("files") if isinstance(inputs, dict) else None
    if isinstance(files, str):
        files = [files]
    if not isinstance(files, list):
        return []
    return [f for f in files if isinstance(f, str) and f.strip()]


def _same_path(a: Any, b: Any) -> bool:
    return isinstance(a, str) and isinstance(b, str) and (
        a.replace("\\", "/").lower() == b.replace("\\", "/").lower()
    )


def recorded_edit(tool_use_result: Any) -> Optional[Tuple[str, List[Hunk], bool]]:
    """``(file path, hunks, created)`` Claude recorded for one successful
    edit, or None (#1349).

    A successful Edit/Write/MultiEdit line carries ``toolUseResult`` with
    ``filePath``, ``structuredPatch`` (hunks with real line numbers) and, for
    a Write, ``type`` — ``"create"`` with an empty patch and the new file
    under ``content``. The shape was probed from the 40 newest transcripts on
    the dev box (key names and types only). A failed call records a plain
    error string instead, so it never reads as an edit. The one reading a
    Chat step and the session's Changed files fold share.
    """
    if not isinstance(tool_use_result, dict):
        return None
    path = tool_use_result.get("filePath")
    if not isinstance(path, str) or not path:
        return None
    created = tool_use_result.get("type") == "create"
    hunks = recorded_hunks(
        tool_use_result.get("structuredPatch"),
        tool_use_result.get("content") if created else None,
    )
    return (path, hunks, created) if hunks else None


def _patch_from_result(call: Optional[Entry], tool_use_result: Any) -> None:
    """Give an Edit/Write/MultiEdit step Claude's own recorded diff (#1349),
    when the line holds this one result and it names the call's own file."""
    action = call.get("action") if call is not None else None
    if not isinstance(action, dict) or action.get("verb") not in ("edited", "wrote"):
        return
    edit = recorded_edit(tool_use_result)
    if edit is None or not _same_path(edit[0], action.get("path")):
        return
    action["added"], action["removed"] = diff_counts(edit[1])
    action["diff"] = cap_diff(edit[1], DIFF_FULL_BYTES)
    # A Write that made the file, so a turn's Edited card badges it A,
    # as the Changed files fold does; Codex's builder sets the same flag.
    if edit[2]:
        action["created"] = True


def claude_entries(lines: List[Line], *, uncapped: bool = False) -> List[Entry]:
    """Typed entries from Claude Code hook-JSONL lines, in file order.

    One transcript line is one content block; assistant ``text`` blocks of
    the same ``message.id`` merge into one ``assistant`` entry (the same rule
    :func:`src.board.transcript_exchange.last_exchange` applies). A ``user`` line is a
    typed prompt when it is a plain string outside
    :data:`src.board.transcript_exchange._SKIP_USER_PREFIXES` and not ``isMeta`` — or a
    list of ``text``/``image`` blocks (an image-paste prompt) with no tool
    result. Tool results pair with their call by ``tool_use_id``. Lines on a
    sidechain (a sub-agent's traffic) keep their kind but carry
    ``sidechain: True`` so the client folds them and pagination doesn't count
    them as turns. Metadata rows (``mode``, ``ai-title``, ``attachment``, …)
    carry no conversation text and are dropped; the exception is an
    ``attachment`` of type ``queued_command`` (a mid-turn message, #1674).

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
            if isinstance(content, list):
                _assistant_blocks(
                    entries, open_calls, assistant_by_mid, content, str(msg.get("id") or ""),
                    offset, ts, sidechain, assistant_cap,
                )
        elif kind == "user":
            _user_line(entries, open_calls, obj, content, offset, ts, sidechain, user_cap)
        elif kind == "attachment":
            _queued_command(entries, obj, offset, ts, sidechain, user_cap)
        elif kind == "system":
            text = str(obj.get("content") or "").strip()
            if text:
                entries.append(_text_entry("system", offset, ts, text, SYSTEM_TEXT_CAP,
                                           label=str(obj.get("subtype") or "system"), sidechain=sidechain))
        # Everything else is metadata without conversation text.
    return entries


def _assistant_blocks(
    entries: List[Entry], open_calls: Dict[str, Entry], assistant_by_mid: Dict[str, Entry],
    content: List[Any], mid: str, offset: int, ts: Any, sidechain: bool, assistant_cap: int,
) -> None:
    """Append one assistant line's text, thinking and tool-call blocks."""
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
            e = _tool_call_entry(entries, block, offset, ts, sidechain)
            entries.append(e)
            if block.get("id"):
                open_calls[str(block["id"])] = e


def _tool_call_entry(entries: List[Entry], block: Dict[str, Any], offset: int, ts: Any, sidechain: bool) -> Entry:
    """One ``tool_use`` block as a ``tool_call`` entry, with its card fields."""
    e = _with_action(_entry(
        "tool_call", offset, ts,
        name=str(block.get("name") or "tool"),
        summary=_tool_summary(block.get("input")),
        result=None, result_truncated=False, sidechain=sidechain,
    ), _tool_action(str(block.get("name") or ""), block.get("input")), _tool_description(block.get("input")))
    if e["name"] == ASK_TOOL_NAME:
        # The Chat pane's question card: the one tool
        # whose structured input is forwarded, plus the id
        # the answer route checks against. An input this
        # reader doesn't recognise keeps the generic row.
        questions = questions_from_input(block.get("input"))
        if questions:
            e["questions"] = questions
            e["call_id"] = str(block.get("id") or "")
    elif e["name"] == SEND_FILE_TOOL:
        # Each listed file is a ref the file route answers
        # (offset of this line, index among its sent files),
        # never the bytes or the full path.
        paths = sent_file_paths(block.get("input"))
        if paths:
            start = sum(len(c.get("sent_files") or []) for c in entries if c.get("offset") == offset)
            e["sent_files"] = [
                {"offset": offset, "n": start + i, "name": clean_name(_basename(p)),
                 "kind": kind_of(_basename(p))}
                for i, p in enumerate(paths)
            ]
            caption = (block.get("input") or {}).get("caption")
            e["caption"] = _cap(str(caption).strip(), SYSTEM_TEXT_CAP)[0] if isinstance(caption, str) else ""
    elif e["name"] == PLAN_TOOL_NAME:
        # The plan card: the whole plan, not the
        # summary's one truncated line of it.
        plan = plan_from_input(block.get("input"))
        if plan:
            e["plan"], e["plan_truncated"] = plan
            e["call_id"] = str(block.get("id") or "")
    return e


def _user_line(
    entries: List[Entry], open_calls: Dict[str, Entry], obj: Dict[str, Any], content: Any,
    offset: int, ts: Any, sidechain: bool, user_cap: int,
) -> None:
    """Append one ``user`` line: its tool results, or the prompt it carries."""
    if isinstance(content, list):
        results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
        if results:
            _tool_results(entries, open_calls, obj, content, len(results), offset, ts, sidechain)
            return
        refs, missing = _image_refs(_line_image_blocks(content), offset, 0)
        text = _with_placeholders(_blocks_text(content), missing)
    elif isinstance(content, str):
        text, refs = content, []
    else:
        return
    stripped = text.strip()
    if not stripped and not refs:
        return
    # `_typed_user_kind` (src/board/transcript_exchange.py) is the one
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


def _tool_results(
    entries: List[Entry], open_calls: Dict[str, Entry], obj: Dict[str, Any], content: List[Any],
    result_count: int, offset: int, ts: Any, sidechain: bool,
) -> None:
    """Pair each ``tool_result`` block of one user line with its open call."""
    tool_use_result = obj.get("toolUseResult")
    # Image numbering walks the whole line, so each
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
        # (`toolUseResult`) or in the rejection text — the picks
        # and the plan outcome; empty for other tools.
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
            # `is_error` beside the error text.
            error=bool(block.get("is_error")),
            decision=decision,
            images=refs,
        )
        if result_count == 1 and not block.get("is_error"):
            _patch_from_result(call, tool_use_result)


def _queued_command(
    entries: List[Entry], obj: Dict[str, Any], offset: int, ts: Any, sidechain: bool, user_cap: int,
) -> None:
    """Append a mid-turn message from its ``queued_command`` attachment.

    A message typed while Claude was mid-turn: the transcript holds no
    ``user`` line for it, only ``queue-operation`` enqueue/remove rows
    (dropped, or it would show twice) and this ``queued_command``
    attachment, written when the text is actually delivered into the running
    turn. Its text is ``prompt`` (a string or a block list). Only a human
    prompt draws: background-task notifications and ``isMeta`` coordinator
    deliveries are harness traffic, the same classification a ``user`` line
    gets.
    """
    attachment = obj.get("attachment") if isinstance(obj.get("attachment"), dict) else {}
    if attachment.get("type") != "queued_command" or attachment.get("commandMode", "prompt") != "prompt":
        return
    text = _blocks_text(attachment.get("prompt")).strip()
    if text and _typed_user_kind(attachment, text) == "typed":
        entries.append(_text_entry("user", offset, ts, text, user_cap, sidechain=sidechain))


def _basename(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or path


def sent_file_at(raw: str, n: int) -> Optional[Tuple[str, Optional[str]]]:
    """``(path as written, the line's cwd)`` of sent file ``n`` on one
    assistant line, else None. Numbered the way :func:`claude_entries` numbers
    them: every ``SendUserFile`` block on the line, in order."""
    obj = _loads(raw)
    if obj is None or obj.get("type") != "assistant" or n < 0:
        return None
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    paths: List[str] = []
    for block in msg.get("content") if isinstance(msg.get("content"), list) else []:
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == SEND_FILE_TOOL:
            paths.extend(sent_file_paths(block.get("input")))
    if n >= len(paths):
        return None
    cwd = obj.get("cwd")
    return paths[n], (cwd if isinstance(cwd, str) and cwd else None)


def _claude_line_key(raw: str) -> Optional[str]:
    """The ``message.id`` of an assistant line — the key that groups one
    message's one-line-per-block records — else None (self-contained line)."""
    obj = _loads(raw)
    if obj is None or obj.get("type") != "assistant":
        return None
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    mid = msg.get("id")
    return str(mid) if mid else None
