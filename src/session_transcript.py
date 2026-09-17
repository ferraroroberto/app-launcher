"""Paginated, typed transcript pages for one live Coding session (#953).

The Coding tab's transcript overlay reads a session's *native* history —
Claude Code's hook JSONL, a Codex rollout, Grok Build's ACP-style
``updates.jsonl`` stream, a Pi session JSONL, or an Antigravity CLI
conversation log (:data:`FLAVORS`) — as an ordered list of typed
entries the phone can render chat-style: typed ``user`` prompts and
``assistant`` replies expanded, everything else (``tool_call`` +
paired result, ``thinking``, ``system`` plumbing, sub-agent traffic) folded.

Every reader here is **bounded**: a long session's JSONL runs to many MB, so
a page is assembled from fixed-size byte windows read *backwards* from a
cursor until enough conversation turns are in hand, and never more than
:data:`REQUEST_BYTE_CAP` per request. The cursor handed back is the byte
offset of the oldest line the page kept, so the next (older) page reads
strictly before it — no turn is duplicated or dropped across pages.

Deliberately **not** on the session-host import closure (CLAUDE.md
"session-host"): this is a webapp-only reader, so a change here is never a
``:8446`` restart concern.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.board_transcript import _SKIP_USER_PREFIXES, _assistant_text

# One backwards read step. 256 KB is the same window the Board's
# last-exchange reader uses (`board_transcript._EXCHANGE_TAIL_BYTES`).
WINDOW_BYTES = 256 * 1024
# Hard ceiling on bytes read for one page, whatever the turn count: a run of
# huge tool results (a multi-MB file Read) must not turn one request into a
# whole-file read. Hitting it returns a short page with a cursor, not an error.
REQUEST_BYTE_CAP = 2 * 1024 * 1024
DEFAULT_LIMIT = 40
MAX_LIMIT = 100

# Display caps — the terminal stays the escape hatch for anything longer.
ASSISTANT_TEXT_CAP = 12_000
USER_TEXT_CAP = 4_000
THINKING_TEXT_CAP = 2_000
TOOL_SUMMARY_CAP = 200
TOOL_RESULT_CAP = 1_500
SYSTEM_TEXT_CAP = 1_500

# Effectively "no cap" for `entry_full_text` (#985): a turn's raw text never
# approaches this, so `_cap` never reports it truncated.
NO_CAP = 1 << 30

# Codex user-role messages that are harness plumbing, not a typed prompt —
# the Codex counterpart of `board_transcript._SKIP_USER_PREFIXES`.
_CODEX_SKIP_USER_PREFIXES = (
    "<environment_context>", "<user_instructions>", "<recommended_plugins>",
    "<skills_instructions>", "<turn_aborted>", "<permissions_instructions>",
    "<collaboration_mode>",
)

# Entry kinds that count as a *conversation turn* for pagination — the
# folded kinds ride along with whichever turns they sit between.
CONVERSATION_KINDS = frozenset({"user", "assistant"})

Line = Tuple[int, str]
Entry = Dict[str, Any]
EntryBuilder = Callable[[List[Line]], List[Entry]]


# ------------------------------------------------------------------ reading


def _read_lines_before(path: Path, start: int, end: int) -> Tuple[List[Line], Optional[int]]:
    """Complete lines of ``path`` that begin in ``[start, end)``.

    Returns ``(lines, oldest_offset)`` — each line as ``(byte offset,
    decoded text)`` in file order, and the offset of the oldest line kept
    (``None`` when the window held no complete line). A line torn at
    ``start`` is dropped — its start lies before the window, so the *next*
    (older) window contains it whole — unless ``start`` is 0 or the byte
    before it is a newline, in which case nothing is torn. Raises OSError on
    a read failure; callers turn that into a distinct "read failed".
    """
    if end <= start:
        return [], None
    read_from = start - 1 if start > 0 else 0
    with path.open("rb") as fh:
        fh.seek(read_from)
        raw = fh.read(end - read_from)
    if start > 0:
        # raw[0] is the byte *before* the window: a newline means the window
        # starts on a line boundary; anything else means a torn first line.
        if raw[:1] == b"\n":
            raw = raw[1:]
            pos = start
        else:
            cut = raw.find(b"\n")
            if cut < 0:
                return [], None
            raw = raw[cut + 1:]
            pos = start + cut  # the dropped prefix spanned [start-1, start+cut)
    else:
        pos = 0
    lines: List[Line] = []
    oldest: Optional[int] = None
    for chunk in raw.split(b"\n"):
        offset = pos
        pos += len(chunk) + 1
        if not chunk.strip():
            continue
        if oldest is None:
            oldest = offset
        lines.append((offset, chunk.decode("utf-8", errors="replace")))
    return lines, oldest


LineKey = Callable[[str], Optional[str]]


def _drop_partial_leading_message(lines: List[Line], line_key: LineKey) -> List[Line]:
    """Drop every message whose span reaches the window's leading edge.

    A window edge can fall inside a multi-line message: Claude writes one
    line per content block under one ``message.id``, and a message's span
    runs from its first to its last line *with tool results (and a
    sub-agent's own messages) in between* — measured on a real transcript,
    1 in 13 messages carries a tool result between its ``tool_use`` and its
    trailing text. The lines before the edge are unread, so a keyed line at
    the edge may be a message's tail: drop through that message's last line,
    and repeat while the new leading line is keyed (a nested sub-agent
    message inside a parent's span). Dropping a message that was in fact
    whole costs nothing — the next, older window re-reads those lines,
    since it ends where the kept lines begin — and guarantees a page never
    opens with half a message.
    """
    keys = [line_key(raw) for _, raw in lines]
    i = 0
    while i < len(lines) and keys[i] is not None:
        key = keys[i]
        last = max(j for j in range(i, len(lines)) if keys[j] == key)
        i = last + 1
    return lines[i:]


def _read_lines_from(path: Path, start: int, span: int, eof: int) -> Tuple[List[Line], int]:
    """Complete lines of ``path`` starting exactly at ``start`` — a known
    line boundary, unlike :func:`_read_lines_before`'s arbitrary window edge
    — for up to ``span`` bytes.

    Returns ``(lines, end)``: ``end`` is the byte position just past the
    last complete line kept. A line torn at the window's far edge is
    dropped (``end`` stops before it) unless the window reached ``eof``, in
    which case the file has nothing more to complete it with, so it counts
    as whole. ``(``[]``, start)`` means nothing complete fit — the caller
    widens ``span`` and reads the same ``start`` again.
    """
    with path.open("rb") as fh:
        fh.seek(start)
        raw = fh.read(span)
    end = start + len(raw)
    if end < eof:
        cut = raw.rfind(b"\n")
        if cut < 0:
            return [], start
        raw = raw[:cut]
        end = start + cut + 1
    lines: List[Line] = []
    pos = start
    for chunk in raw.split(b"\n"):
        offset = pos
        pos += len(chunk) + 1
        if chunk.strip():
            lines.append((offset, chunk.decode("utf-8", errors="replace")))
    return lines, end


def entry_full_text(path: Path, offset: int, flavor: str) -> Optional[Dict[str, Any]]:
    """The uncapped text of the turn entry beginning at byte ``offset``.

    The counterpart of a capped, ``truncated: true`` entry from
    :func:`transcript_page` — the copy-full-text route (#985) calls this
    when the phone wants the whole thing. A ``user`` entry is always one
    line; a Claude ``assistant`` entry can span several (one line per
    content block, same ``message.id``), so this reads forward from
    ``offset`` — mirroring :func:`_drop_partial_leading_message`'s span
    logic in the opposite direction — until a read line past the last one
    keyed to that message confirms nothing more will merge into it, or the
    read hits :data:`REQUEST_BYTE_CAP` (a message that huge is already a
    pathological case; the result then stays best-effort and reports
    itself ``truncated`` rather than blocking on an unbounded read).

    Returns ``None`` when ``offset`` no longer starts a turn there — the
    file was rotated/truncated since the page was served, or the offset
    was never one to begin with.
    """
    build, line_key = _flavor(flavor)
    eof = path.stat().st_size
    if offset < 0 or offset >= eof:
        return None
    lines: List[Line] = []
    pos = offset
    target_key: Optional[str] = None
    bytes_read = 0
    hit_cap = False
    span = WINDOW_BYTES
    while True:
        more, new_pos = _read_lines_from(path, pos, span, eof)
        if not more and new_pos == pos:
            span *= 2
            if bytes_read + span > REQUEST_BYTE_CAP:
                hit_cap = True
                break
            continue
        bytes_read += new_pos - pos
        pos = new_pos
        if not lines and more:
            target_key = line_key(more[0][1])
        lines.extend(more)
        if target_key is None:
            break  # a single-line entry (user, or any non-assistant kind)
        last_match = max(
            (i for i, (_, raw) in enumerate(lines) if line_key(raw) == target_key),
            default=-1,
        )
        if last_match < len(lines) - 1:
            break  # a later, differently-keyed line confirms the span closed
        if pos >= eof:
            break
        if bytes_read >= REQUEST_BYTE_CAP:
            hit_cap = True
            break
        span = WINDOW_BYTES
    if not lines or lines[0][0] != offset:
        return None
    entries = build(lines, uncapped=True)
    for e in entries:
        if e.get("offset") == offset and _is_turn(e):
            return {"text": e["text"], "truncated": bool(e.get("truncated")) or hit_cap}
    return None


def _page(
    path: Path, before: Optional[int], limit: int, build: EntryBuilder, line_key: LineKey
) -> Dict[str, Any]:
    """Assemble one page of entries ending at byte ``before``.

    Reads :data:`WINDOW_BYTES` windows backwards until ``limit``
    conversation turns (``user`` + ``assistant`` entries) are collected, the
    file start is reached, or :data:`REQUEST_BYTE_CAP` bytes have been read.
    The page is then trimmed at a turn boundary so it holds at most
    ``limit`` turns, and ``next_cursor`` is the offset of the oldest line it
    kept (``None`` once the file start is included). A page never opens
    inside a multi-line message, and a cut never splits one.
    """
    size = path.stat().st_size
    end = size if before is None or before > size else max(0, before)
    limit = max(1, min(int(limit), MAX_LIMIT))
    collected: List[Line] = []
    bytes_read = 0
    span = WINDOW_BYTES
    while True:
        start = max(0, end - span)
        lines, oldest = _read_lines_before(path, start, end)
        bytes_read += end - start
        if start == 0:
            collected = lines + collected
            break
        if oldest is not None:
            lines = _drop_partial_leading_message(lines, line_key)
        if not lines:
            # Nothing whole in the window — a single record wider than it (an
            # image-paste prompt carries base64 blocks), or one message
            # spanning it. Widen *in place* — `end` stays put — so it is
            # eventually read whole; sliding `end` into its middle would tear
            # its tail off for good.
            span *= 2
            continue
        collected = lines + collected
        if _turns(build(collected)) >= limit or bytes_read >= REQUEST_BYTE_CAP:
            break
        # Next window ends where the oldest kept line begins (the lines
        # dropped above it get re-read whole).
        end = lines[0][0]
        span = WINDOW_BYTES

    entries = build(collected)
    # Trim to `limit` turns at a turn boundary: drop every raw line before the
    # line that opens the oldest surviving turn — pulled back to that
    # message's first line so its thinking/tool blocks stay with its text —
    # then rebuild so a tool result whose call fell off the page stands on
    # its own instead of vanishing. Sidechain turns are never cut points
    # (`_is_turn`), so the cut message is a top-level one, which nothing
    # else's span contains.
    # A page that includes the file start and fits in `limit` keeps every
    # line; any other page is trimmed to its oldest turn — even at exactly
    # `limit` turns — so it never opens at a raw window edge that may fall
    # between a tool call and its result. (A page with no turn at all — a
    # multi-MB autonomous stretch under the byte cap — keeps the edge.)
    turn_offsets = [e["offset"] for e in entries if _is_turn(e)]
    whole_file = start == 0 and len(turn_offsets) <= limit
    if turn_offsets and not whole_file:
        cut = turn_offsets[max(0, len(turn_offsets) - limit)]
        idx = next(i for i, ln in enumerate(collected) if ln[0] == cut)
        cut_key = line_key(collected[idx][1])
        if cut_key is not None:
            idx = next(i for i, ln in enumerate(collected) if line_key(ln[1]) == cut_key)
        collected = collected[idx:]
        entries = build(collected)
    # The next page reads strictly before the oldest line this one kept;
    # None once that line is the file's first (an empty `collected` means an
    # empty file — the loop only exits without lines at offset 0).
    next_cursor: Optional[int] = collected[0][0] if collected else None
    if next_cursor == 0:
        next_cursor = None
    # A turn keeps its offset — the copy-full-text route (#985) needs it to
    # ask for the uncapped entry; the folded kinds have no such use and stay
    # unexposed, same as before.
    for e in entries:
        if not _is_turn(e):
            e.pop("offset", None)
    return {"entries": entries, "next_cursor": next_cursor}


def _is_turn(entry: Entry) -> bool:
    return entry.get("kind") in CONVERSATION_KINDS and not entry.get("sidechain")


def _turns(entries: List[Entry]) -> int:
    return sum(1 for e in entries if _is_turn(e))


# ------------------------------------------------------------------ helpers


def _cap(text: str, cap: int) -> Tuple[str, bool]:
    text = text or ""
    if len(text) <= cap:
        return text, False
    return text[:cap], True


def _loads(raw: str) -> Optional[Dict[str, Any]]:
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _tool_summary(inputs: Any) -> str:
    """One line describing a tool call's input: the first string-valued
    argument (a command, a path, a prompt), else a compact key list."""
    if isinstance(inputs, str):
        text = inputs
    elif isinstance(inputs, dict):
        text = ""
        for value in inputs.values():
            if isinstance(value, str) and value.strip():
                text = value.strip()
                break
        if not text:
            text = ", ".join(str(k) for k in inputs.keys())
    else:
        text = ""
    text = " ".join(text.split())
    return _cap(text, TOOL_SUMMARY_CAP)[0]


def _blocks_text(content: Any, *, types: Tuple[str, ...] = ("text",), key: str = "text") -> str:
    """Join the ``text`` of the content blocks whose type is in ``types``;
    a plain string is returned as-is."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") in types:
            text = str(block.get(key) or "").strip()
            if text:
                parts.append(text)
    return "\n\n".join(parts)


def _entry(kind: str, offset: int, timestamp: Any, **fields: Any) -> Entry:
    e: Entry = {"kind": kind, "offset": offset, "timestamp": timestamp}
    e.update(fields)
    return e


def _text_entry(kind: str, offset: int, timestamp: Any, text: str, cap: int, **fields: Any) -> Entry:
    body, truncated = _cap(text.strip(), cap)
    return _entry(kind, offset, timestamp, text=body, truncated=truncated, **fields)


def _attach_result(entries: List[Entry], calls: Dict[str, Entry], call_id: Any,
                   text: str, offset: int, timestamp: Any, sidechain: bool) -> None:
    body, truncated = _cap(text.strip(), TOOL_RESULT_CAP)
    call = calls.pop(str(call_id), None) if call_id else None
    if call is not None and call.get("result") is None:
        call["result"] = body
        call["result_truncated"] = truncated
        return
    # A result whose call fell off this page (or an unknown id): stands alone
    # so it is neither lost nor mis-paired.
    entries.append(_entry(
        "tool_result", offset, timestamp, text=body, truncated=truncated,
        tool_use_id=str(call_id or ""), sidechain=sidechain,
    ))


def _harness_label(text: str) -> str:
    stripped = text.lstrip()
    if stripped.startswith("<system-reminder"):
        return "system-reminder"
    if stripped.startswith("<task-notification"):
        return "task-notification"
    if stripped.startswith("<command-") or stripped.startswith("<local-command-"):
        return "command"
    return "system"


# ------------------------------------------------------------ Claude JSONL


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
    :func:`entry_full_text` — the folded kinds keep their normal caps either
    way, since only a turn's own text is ever read back from an uncapped call.
    """
    assistant_cap = NO_CAP if uncapped else ASSISTANT_TEXT_CAP
    user_cap = NO_CAP if uncapped else USER_TEXT_CAP
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
                    e = _entry(
                        "tool_call", offset, ts,
                        name=str(block.get("name") or "tool"),
                        summary=_tool_summary(block.get("input")),
                        result=None, result_truncated=False, sidechain=sidechain,
                    )
                    entries.append(e)
                    if block.get("id"):
                        open_calls[str(block["id"])] = e
            continue

        if kind == "user":
            if isinstance(content, list):
                results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
                if results:
                    for block in results:
                        _attach_result(
                            entries, open_calls, block.get("tool_use_id"),
                            _blocks_text(block.get("content")), offset, ts, sidechain,
                        )
                    continue
                text = _blocks_text(content)
                images = sum(1 for b in content if isinstance(b, dict) and b.get("type") == "image")
                if images:
                    text = (text + "\n\n" if text else "") + " ".join(["[image]"] * images)
            elif isinstance(content, str):
                text = content
            else:
                continue
            stripped = text.strip()
            if not stripped:
                continue
            if obj.get("isMeta"):
                entries.append(_text_entry("system", offset, ts, stripped, SYSTEM_TEXT_CAP,
                                           label="injected", sidechain=sidechain))
            elif any(stripped.startswith(p) for p in _SKIP_USER_PREFIXES):
                entries.append(_text_entry("system", offset, ts, stripped, SYSTEM_TEXT_CAP,
                                           label=_harness_label(stripped), sidechain=sidechain))
            else:
                entries.append(_text_entry("user", offset, ts, stripped, user_cap, sidechain=sidechain))
            continue

        if kind == "system":
            text = str(obj.get("content") or "").strip()
            if text:
                entries.append(_text_entry("system", offset, ts, text, SYSTEM_TEXT_CAP,
                                           label=str(obj.get("subtype") or "system"), sidechain=sidechain))
            continue
        # Everything else is metadata without conversation text.
    return entries


# ------------------------------------------------------------- Codex JSONL


def codex_entries(lines: List[Line], *, uncapped: bool = False) -> List[Entry]:
    """Typed entries from a Codex rollout's ``response_item`` lines.

    ``message`` payloads carry ``input_text`` (user/developer) or
    ``output_text`` (assistant) blocks — developer-role and harness-tagged
    user messages are ``system``. ``function_call`` / ``custom_tool_call``
    pair with their ``*_output`` by ``call_id``; ``reasoning`` summaries
    become ``thinking``; ``agent_message`` (sub-agent mailbox traffic) is a
    sidechain ``system`` entry. Everything else (token usage, events, world
    state) is dropped.

    ``uncapped`` — see :func:`claude_entries`.
    """
    assistant_cap = NO_CAP if uncapped else ASSISTANT_TEXT_CAP
    user_cap = NO_CAP if uncapped else USER_TEXT_CAP
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
            e = _entry(
                "tool_call", offset, ts,
                name=str(payload.get("name") or "tool"),
                summary=_tool_summary(payload.get("arguments") if pt == "function_call" else payload.get("input")),
                result=None, result_truncated=False, sidechain=False,
            )
            entries.append(e)
            if payload.get("call_id"):
                open_calls[str(payload["call_id"])] = e
        elif pt in ("function_call_output", "custom_tool_call_output"):
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


# ----------------------------------------------------- Grok updates.jsonl

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


def _grok_result_text(update: Dict[str, Any]) -> str:
    """The text of a completed ``tool_call_update``.

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

    ``uncapped`` — see :func:`claude_entries`.
    """
    assistant_cap = NO_CAP if uncapped else ASSISTANT_TEXT_CAP
    user_cap = NO_CAP if uncapped else USER_TEXT_CAP
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
            entry = _entry(
                "tool_call", offset, ts,
                name=str(update.get("title") or "tool"),
                summary=_tool_summary(update.get("rawInput")),
                result=None, result_truncated=False, sidechain=False,
            )
            entries.append(entry)
            if update.get("toolCallId"):
                open_calls[str(update["toolCallId"])] = entry
        elif kind == "tool_call_update" and update.get("status") == "completed":
            # The other half of a `tool_call_update` (no `status`, carrying
            # `locations`/`kind`) only restates the call, so it is dropped.
            _attach_result(
                entries, open_calls, update.get("toolCallId"),
                _grok_result_text(update), offset, ts, False,
            )
        # `turn_completed` and `hook_execution` carry no conversation text.
    return entries


def _grok_line_key(raw: str) -> Optional[str]:
    """Grok records are self-contained: one line is one whole segment
    (see :func:`grok_entries`), so no line ever needs another to be read."""
    return None


# ------------------------------------------------------------- Pi JSONL

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
    :func:`claude_entries` does for an image-paste prompt.
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
      :func:`entry_full_text` resolves against.

    Timestamps come from the **outer** record's ISO string, not the inner
    ``message.timestamp`` (epoch milliseconds), so a turn renders as itself
    rather than as 1970.

    Known omission, shared with the Codex and Grok flavours: a failed tool
    result (``isError: true``) reads the same as a successful one, because
    the ``Entry`` contract has no error field and the client renders none.

    ``uncapped`` — see :func:`claude_entries`.
    """
    assistant_cap = NO_CAP if uncapped else ASSISTANT_TEXT_CAP
    user_cap = NO_CAP if uncapped else USER_TEXT_CAP
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
                    call = _entry(
                        "tool_call", offset, ts,
                        name=str(block.get("name") or "tool"),
                        summary=_tool_summary(block.get("arguments")),
                        result=None, result_truncated=False, sidechain=False,
                    )
                    entries.append(call)
                    if block.get("id"):
                        open_calls[str(block["id"])] = call
            continue

        if role == "toolResult":
            text, images = _pi_blocks(content)
            if images:
                text = (text + "\n\n" if text else "") + " ".join(["[image]"] * images)
            _attach_result(entries, open_calls, msg.get("toolCallId"), text,
                           offset, ts, False)
            continue

        if role == "user":
            text, images = _pi_blocks(content)
            if images:
                text = (text + "\n\n" if text else "") + " ".join(["[image]"] * images)
            stripped = text.strip()
            if not stripped:
                continue
            if any(stripped.startswith(p) for p in _PI_SKIP_USER_PREFIXES):
                label = "skill" if stripped.startswith("<skill name=") else _harness_label(stripped)
                entries.append(_text_entry("system", offset, ts, stripped, SYSTEM_TEXT_CAP,
                                           label=label, sidechain=False))
            else:
                entries.append(_text_entry("user", offset, ts, stripped, user_cap,
                                           sidechain=False))
    return entries


def _pi_line_key(raw: str) -> Optional[str]:
    """Pi records are self-contained: one line carries a message's whole
    content list (see :func:`pi_entries`), so no line ever needs another."""
    return None


# Antigravity's `SYSTEM`-sourced steps are harness plumbing; the label is
# what the folded `system` card shows. A step type absent here still folds,
# under the generic label, so a new one is never rendered as a reply.
_AGY_SYSTEM_LABELS = {
    "EPHEMERAL_MESSAGE": "ephemeral",
    "SYSTEM_MESSAGE": "system-message",
    "CHECKPOINT": "compaction",
    "ERROR_MESSAGE": "error",
    "CONVERSATION_HISTORY": "history",
}
# A typed prompt arrives wrapped, followed by `<ADDITIONAL_METADATA>` (local
# time) and sometimes `<USER_SETTINGS_CHANGE>` — only the request is shown.
_AGY_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)
# Tool-argument keys that are Antigravity's own UI labels rather than
# arguments ("Listing directory contents"), so they never stand in for the
# command or path a tool card should show.
_AGY_LABEL_ARGS = ("toolAction", "toolSummary")
# Every tool result opens with the harness's own timing header — 430 of 430
# on this box — which is two lines of boilerplate on a phone card and two
# lines of the result cap. The `Completed At` line is absent while a tool is
# still running.
_AGY_RESULT_HEADER_RE = re.compile(
    r"\ACreated At: [^\n]*\n(?:Completed At: [^\n]*\n)?"
)


def _agy_arg(value: Any) -> Any:
    """One tool argument, undoing ``transcript.jsonl``'s second JSON encode.

    The flat file writes each argument's value re-encoded (``"\\"C:\\\\\\\\x\\""``)
    while ``transcript_full.jsonl`` writes it plain; a reader that hits the
    fallback file would otherwise show the quotes and doubled backslashes.
    """
    if isinstance(value, str) and len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _agy_tool_summary(args: Any) -> str:
    """A tool call's one-line summary: the command, path or prompt.

    :func:`_tool_summary`'s first-string-argument rule, applied after the UI
    labels are dropped — so a card reads ``run_command`` / the command line,
    as the Claude and Pi flavours do, rather than ``File type check``. The
    labels are the fallback for a call whose real arguments are all
    non-strings.
    """
    if not isinstance(args, dict):
        return _tool_summary(_agy_arg(args))
    unwrapped = {key: _agy_arg(value) for key, value in args.items()}
    labels = {key: unwrapped.pop(key, "") for key in _AGY_LABEL_ARGS}
    if any(isinstance(value, str) and value.strip() for value in unwrapped.values()):
        return _tool_summary(unwrapped)
    return _tool_summary(_agy_arg(labels.get("toolSummary"))) or _tool_summary(unwrapped)


def _agy_mark(entry: Entry, field: str, cut: Any) -> Entry:
    """Flag ``entry`` truncated when the harness itself cut ``field``
    (``truncated_fields``, written only by the fallback flat file)."""
    if field in cut:
        entry["truncated"] = True
    return entry


def antigravity_entries(lines: List[Line], *, uncapped: bool = False) -> List[Entry]:
    """Typed entries from an Antigravity CLI conversation log, in file order.

    One JSON step per line — ``{"step_index", "source", "type", "status",
    "created_at", "content"?, "thinking"?, "tool_calls"?}`` — so no entry
    ever needs a neighbouring line (:func:`_antigravity_line_key`) and a
    line yields at most one turn, which is what :func:`entry_full_text`
    resolves against. Measured on a session launched through the launcher's
    own API for #1014 plus the 264 conversations already on disk:

    * A ``PLANNER_RESPONSE`` carries ``thinking``, ``content`` and
      ``tool_calls`` in any combination — when the model calls a tool there
      is usually **no** ``content`` at all. All three come from one line at
      one offset, so nothing is merged across records and a reply can never
      be hoisted above the call that produced it (the trap #1012 hit).
    * **A tool result is the next ``MODEL`` step that is not a
      ``PLANNER_RESPONSE``, and its type does not name the tool** — a
      ``list_dir`` call answered as ``GENERIC``, not ``LIST_DIRECTORY``.
      Its text opens with the harness's ``Created At:`` / ``Completed At:``
      timing header, dropped here so the card shows the output itself.
      There is no call id anywhere, so calls and results pair positionally,
      FIFO. That held for 207 of the 210 logs on disk; the other three end
      on an unanswered call (which simply keeps ``result: None``), and six
      have a dropped step, whose orphan result stands alone rather than
      mis-pairing.
    * ``SYSTEM``-sourced steps are plumbing (ephemeral reminders, a
      compaction checkpoint, a tool-call parse error) and fold as
      ``system``; ``CONVERSATION_HISTORY`` carries no ``content`` and so
      produces nothing.
    * Timestamps are ISO strings already, in a mix of ``Z`` and local-offset
      forms within one file, both of which the client's ``new Date`` reads —
      no epoch normalisation is needed here, unlike Grok's (#1012).

    Known omission, shared with the Codex, Grok and Pi flavours (#1020): a
    failed tool result reads the same as a successful one. Antigravity gives
    a reader nothing structured to tell them apart — a command that exits 1
    is still ``status: "DONE"``, with the failure only in the result's
    English prose.

    ``uncapped`` — see :func:`claude_entries`. On the fallback flat file
    ``truncated_fields`` names fields the harness itself already cut, and
    those entries report ``truncated`` however high the cap.
    """
    assistant_cap = NO_CAP if uncapped else ASSISTANT_TEXT_CAP
    user_cap = NO_CAP if uncapped else USER_TEXT_CAP
    entries: List[Entry] = []
    open_calls: Dict[str, Entry] = {}
    unanswered: List[str] = []
    for offset, raw in lines:
        obj = _loads(raw)
        if obj is None:
            continue
        step_type = str(obj.get("type") or "")
        cut = obj.get("truncated_fields")
        cut = set(cut) if isinstance(cut, list) else set()
        content = str(obj.get("content") or "")
        ts = obj.get("created_at")

        if step_type == "USER_INPUT":
            match = _AGY_REQUEST_RE.search(content)
            text = (match.group(1) if match else content).strip()
            if text:
                entries.append(_agy_mark(_text_entry(
                    "user", offset, ts, text, user_cap, sidechain=False), "content", cut))
            continue

        if str(obj.get("source") or "") == "SYSTEM":
            text = content.strip() or str(obj.get("error") or "").strip()
            if text:
                entries.append(_text_entry(
                    "system", offset, ts, text, SYSTEM_TEXT_CAP,
                    label=_AGY_SYSTEM_LABELS.get(step_type, "system"), sidechain=False,
                ))
            continue

        if step_type == "PLANNER_RESPONSE":
            thinking = str(obj.get("thinking") or "").strip()
            if thinking:
                entries.append(_agy_mark(_text_entry(
                    "thinking", offset, ts, thinking, THINKING_TEXT_CAP,
                    sidechain=False), "thinking", cut))
            if content.strip():
                entries.append(_agy_mark(_text_entry(
                    "assistant", offset, ts, content, assistant_cap,
                    sidechain=False), "content", cut))
            calls = obj.get("tool_calls")
            for index, call in enumerate(calls if isinstance(calls, list) else []):
                if not isinstance(call, dict):
                    continue
                key = f"{offset}:{index}"
                entry = _entry(
                    "tool_call", offset, ts,
                    name=str(_agy_arg(call.get("name")) or "tool"),
                    summary=_agy_tool_summary(call.get("args")),
                    result=None, result_truncated=False, sidechain=False,
                )
                entries.append(entry)
                open_calls[key] = entry
                unanswered.append(key)
            continue

        # Every remaining MODEL step is a tool result, for the oldest call
        # still waiting on one.
        _attach_result(entries, open_calls, unanswered.pop(0) if unanswered else None,
                       _AGY_RESULT_HEADER_RE.sub("", content), offset, ts, False)
    return entries


def _antigravity_line_key(raw: str) -> Optional[str]:
    """Antigravity writes one self-contained step per line (see
    :func:`antigravity_entries`), so no line ever needs another."""
    return None


def _claude_line_key(raw: str) -> Optional[str]:
    """The ``message.id`` of an assistant line — the key that groups one
    message's one-line-per-block records — else None (self-contained line)."""
    obj = _loads(raw)
    if obj is None or obj.get("type") != "assistant":
        return None
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    mid = msg.get("id")
    return str(mid) if mid else None


def _codex_line_key(raw: str) -> Optional[str]:
    """Codex rollout records are self-contained: never grouped."""
    return None


# The line grammars this reader understands: flavour name → (entry builder,
# line key). One table so a new harness is one row here plus its parser,
# rather than a branch in every dispatch site. The names are the endpoint's
# `_FLAVOR_BY_AGENT` values (app/webapp/routers/session_transcript.py).
FLAVORS: Dict[str, Tuple[EntryBuilder, LineKey]] = {
    "claude": (claude_entries, _claude_line_key),
    "codex": (codex_entries, _codex_line_key),
    "grok": (grok_entries, _grok_line_key),
    "pi": (pi_entries, _pi_line_key),
    "antigravity": (antigravity_entries, _antigravity_line_key),
}


def _flavor(flavor: str) -> Tuple[EntryBuilder, LineKey]:
    """The builder + line key for ``flavor``, defaulting to Claude's — the
    same fallback the endpoint applies to a session with no agent field."""
    return FLAVORS.get(flavor, FLAVORS["claude"])


# ---------------------------------------------------------------- public


def transcript_page(
    path: Any,
    *,
    before: Optional[int] = None,
    limit: int = DEFAULT_LIMIT,
    flavor: str = "claude",
) -> Dict[str, Any]:
    """One page of typed entries from ``path``, newest-last.

    ``before`` is a byte offset (the previous page's ``next_cursor``; ``None``
    = the end of the file); ``limit`` is the maximum number of conversation
    turns (``user`` + ``assistant`` entries — roughly two per exchange).
    ``flavor`` picks the line grammar — see :data:`FLAVORS`. Raises
    ``OSError`` when the file can't be read — a distinct condition from "no
    transcript", which the caller establishes *before* calling.
    """
    build, line_key = _flavor(flavor)
    return _page(Path(str(path)), before, limit, build, line_key)
