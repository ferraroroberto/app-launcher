"""Coding-tab session transcript (issue #953) — parser, pagination, router.

  * ``session_transcript.claude_entries`` — typed entries from hook JSONL:
    assistant text merged across lines of one ``message.id``, tool results
    paired to their call, harness wrappers / ``isMeta`` bodies / ``system``
    rows typed as folded ``system``, image-paste prompts typed as ``user``,
    sidechain lines flagged, metadata rows dropped, display caps.
  * ``session_transcript.transcript_page`` — bounded backwards paging: a
    torn line at a window edge is never duplicated or dropped, a record wider
    than the window is read whole, the per-request byte cap yields a short
    page with a cursor, and the concatenation of every page equals one full
    parse.
  * ``session_transcript.codex_entries`` — the rollout grammar.
  * ``session_transcript.grok_entries`` — the Grok Build ``updates.jsonl``
    grammar (#1012), driven by a captured session: chunk kinds, tool calls
    paired with their completion, the three result shapes, epoch stamps
    normalised to ISO, and consecutive-only chunk coalescing.
  * ``GET /api/claude-code/sessions/{sid}/transcript`` — passkey-gated,
    distinct unavailable reasons, no transcript bodies in the log.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src import board, session_transcript as st


# ------------------------------------------------------------ fixtures


def _write_jsonl(path: Path, lines: List[Dict[str, Any]]) -> Path:
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )
    return path


def _user(text: Any, **extra: Any) -> Dict[str, Any]:
    row = {"type": "user", "timestamp": "2026-09-14T10:00:00Z",
           "message": {"role": "user", "content": text}}
    row.update(extra)
    return row


def _assistant(blocks: List[Dict[str, Any]], msg_id: str = "m1", **extra: Any) -> Dict[str, Any]:
    row = {"type": "assistant", "timestamp": "2026-09-14T10:00:01Z",
           "message": {"id": msg_id, "role": "assistant", "content": blocks}}
    row.update(extra)
    return row


def _tool_use(name: str, inputs: Dict[str, Any], tid: str) -> Dict[str, Any]:
    return {"type": "tool_use", "id": tid, "name": name, "input": inputs}


def _tool_result(tid: str, content: Any) -> Dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": tid, "content": content}


def _parse_whole(path: Path, build=st.claude_entries) -> List[Dict[str, Any]]:
    """Reference: one parse of the whole file — offset stripped from the
    folded kinds, kept on turns, matching :func:`session_transcript._page`
    (#985)."""
    raw = path.read_bytes()
    lines, pos = [], 0
    for chunk in raw.split(b"\n"):
        if chunk.strip():
            lines.append((pos, chunk.decode("utf-8", errors="replace")))
        pos += len(chunk) + 1
    entries = build(lines)
    for e in entries:
        if not st._is_turn(e):
            e.pop("offset", None)
    return entries


def _walk(path: Path, limit: int, flavor: str = "claude") -> List[List[Dict[str, Any]]]:
    """Every page from newest to oldest, following ``next_cursor``."""
    pages, cursor, guard = [], None, 0
    while True:
        page = st.transcript_page(path, before=cursor, limit=limit, flavor=flavor)
        pages.append(page["entries"])
        cursor = page["next_cursor"]
        guard += 1
        assert guard < 10_000, "cursor never reached the file start"
        if cursor is None:
            return pages


def _kinds(entries: List[Dict[str, Any]]) -> List[str]:
    return [e["kind"] for e in entries]


# -------------------------------------------------------- claude_entries


def test_claude_entries_types_every_shape(tmp_path: Path):
    path = _write_jsonl(tmp_path / "t.jsonl", [
        {"type": "mode", "mode": "normal"},                       # metadata: dropped
        _user("<command-name>/issue-start</command-name>"),      # harness wrapper
        _user("Base directory for this skill: …", isMeta=True),  # injected skill body
        _user("<system-reminder>ctx</system-reminder>"),
        _user("Please fix the bug"),
        _assistant([{"type": "thinking", "thinking": "hmm"}], "m1"),
        _assistant([{"type": "text", "text": "On it."}], "m1"),
        _assistant([_tool_use("Bash", {"command": "pytest -q", "timeout": 5}, "t1")], "m1"),
        _user([_tool_result("t1", "3 passed")]),
        _assistant([{"type": "text", "text": "All green."}], "m1"),
        _assistant([{"type": "text", "text": "Sub-agent says hi"}], "m9", isSidechain=True),
        {"type": "system", "subtype": "compact_boundary", "content": "Conversation compacted",
         "timestamp": "2026-09-14T10:00:05Z"},
        {"type": "system", "subtype": "turn_duration", "durationMs": 12},  # no content: dropped
        _user([{"type": "text", "text": "Look at this"}, {"type": "image", "source": {}}]),
        _assistant([{"type": "text", "text": "Nice screenshot."}], "m2"),
    ])
    entries = _parse_whole(path)
    assert _kinds(entries) == [
        "system", "system", "system", "user", "thinking", "assistant",
        "tool_call", "assistant", "system", "user", "assistant",
    ]
    system = [e for e in entries if e["kind"] == "system"]
    assert [e["label"] for e in system] == ["command", "injected", "system-reminder", "compact_boundary"]
    assert entries[3]["text"] == "Please fix the bug"
    # Both text blocks of m1 merge into one entry, in place, with the
    # tool call between them staying where it was written.
    assistant_m1 = entries[5]
    assert assistant_m1["text"] == "On it.\n\nAll green."
    call = entries[6]
    assert call["name"] == "Bash"
    assert call["summary"] == "pytest -q"          # first string-valued input
    assert call["result"] == "3 passed"
    assert call["result_truncated"] is False
    sidechain = entries[7]
    assert sidechain["sidechain"] is True and sidechain["text"] == "Sub-agent says hi"
    assert entries[9]["text"] == "Look at this\n\n[image]"
    assert all(e["timestamp"] for e in entries)


def test_claude_entries_caps_long_bodies(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(st, "ASSISTANT_TEXT_CAP", 20)
    monkeypatch.setattr(st, "TOOL_RESULT_CAP", 5)
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _assistant([{"type": "text", "text": "x" * 15}], "m1"),
        _assistant([{"type": "text", "text": "y" * 15}], "m1"),
        _assistant([_tool_use("Read", {"file_path": "a.py"}, "t1")], "m1"),
        _user([_tool_result("t1", [{"type": "text", "text": "0123456789"}])]),
    ])
    entries = _parse_whole(path)
    assert entries[0]["truncated"] is True and len(entries[0]["text"]) == 20
    assert entries[1]["result"] == "01234" and entries[1]["result_truncated"] is True


def test_unknown_tool_result_stands_alone(tmp_path: Path):
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _user([_tool_result("ghost", "orphan output")]),
    ])
    entries = _parse_whole(path)
    assert _kinds(entries) == ["tool_result"]
    assert entries[0]["tool_use_id"] == "ghost" and entries[0]["text"] == "orphan output"


# ------------------------------------------------------- transcript_page


def _conversation(turns: int, *, tool_calls_per_turn: int = 2) -> List[Dict[str, Any]]:
    lines: List[Dict[str, Any]] = []
    for i in range(turns):
        lines.append(_user(f"prompt {i} " + "p" * (i % 7)))
        mid = f"m{i}"
        lines.append(_assistant([{"type": "thinking", "thinking": f"think {i}"}], mid))
        lines.append(_assistant([{"type": "text", "text": f"reply {i} opener"}], mid))
        for k in range(tool_calls_per_turn):
            tid = f"t{i}-{k}"
            lines.append(_assistant([_tool_use("Bash", {"command": f"cmd {i} {k}"}, tid)], mid))
            lines.append(_user([_tool_result(tid, "out " + "o" * ((i * k) % 50))]))
        lines.append(_assistant([{"type": "text", "text": f"reply {i} closer " + "r" * (i % 11)}], mid))
    return lines


@pytest.mark.parametrize("limit", [1, 7, 40])
def test_pages_concatenate_to_one_full_parse(tmp_path: Path, monkeypatch, limit: int):
    """No turn duplicated or dropped across pages, whatever the page size —
    with the read window shrunk so every window edge tears a line."""
    monkeypatch.setattr(st, "WINDOW_BYTES", 300)
    path = _write_jsonl(tmp_path / "t.jsonl", _conversation(60))
    pages = _walk(path, limit)
    assert len(pages) > 1
    for page in pages:
        assert sum(1 for e in page if e["kind"] in ("user", "assistant")) <= limit
    joined = [e for page in reversed(pages) for e in page]
    assert joined == _parse_whole(path)


def test_first_page_is_the_newest_turns_and_cursor_advances(tmp_path: Path):
    """``limit`` counts user + assistant entries — two per exchange here."""
    path = _write_jsonl(tmp_path / "t.jsonl", _conversation(10))
    page = st.transcript_page(path, limit=4)
    users = [e["text"] for e in page["entries"] if e["kind"] == "user"]
    assert users == ["prompt 8 p", "prompt 9 pp"]
    # The exchange's thinking + tool blocks ride with their message.
    assert _kinds(page["entries"])[:4] == ["user", "thinking", "assistant", "tool_call"]
    assert page["next_cursor"] is not None
    older = st.transcript_page(path, before=page["next_cursor"], limit=4)
    assert [e["text"] for e in older["entries"] if e["kind"] == "user"] == ["prompt 6 pppppp", "prompt 7"]


def test_whole_file_in_one_page_has_no_cursor(tmp_path: Path):
    path = _write_jsonl(tmp_path / "t.jsonl", _conversation(3))
    page = st.transcript_page(path, limit=40)
    assert page["next_cursor"] is None
    assert page["entries"] == _parse_whole(path)


def test_record_wider_than_the_window_is_read_whole(tmp_path: Path, monkeypatch):
    """The smoke-test regression: an image-paste prompt is one JSONL line of
    hundreds of KB. Sliding the window into its middle used to tear its tail
    off and lose the whole turn."""
    monkeypatch.setattr(st, "WINDOW_BYTES", 200)
    big = "B" * 5_000
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _user("first"),
        _assistant([{"type": "text", "text": "ok"}], "m0"),
        _user(big),
        _assistant([{"type": "text", "text": "seen it"}], "m1"),
        _user("last"),
        _assistant([{"type": "text", "text": "bye"}], "m2"),
    ])
    pages = _walk(path, 1)
    joined = [e for page in reversed(pages) for e in page]
    assert [e["text"][:5] for e in joined if e["kind"] == "user"] == ["first", "BBBBB", "last"]
    assert joined == _parse_whole(path)


def test_byte_cap_yields_a_short_page_with_a_cursor(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(st, "WINDOW_BYTES", 300)
    monkeypatch.setattr(st, "REQUEST_BYTE_CAP", 1_200)
    path = _write_jsonl(tmp_path / "t.jsonl", _conversation(40))
    page = st.transcript_page(path, limit=100)
    turns = sum(1 for e in page["entries"] if e["kind"] in ("user", "assistant"))
    assert 0 < turns < 80, "the cap must cut the page well short of the 100 asked"
    assert page["next_cursor"] is not None
    # …and the walk still completes losslessly.
    pages = _walk(path, 100)
    assert [e for page in reversed(pages) for e in page] == _parse_whole(path)


def test_pair_split_across_a_page_boundary_keeps_the_result(tmp_path: Path):
    """A tool call whose result lands on the newer page: the result stands
    alone there (never lost), the call shows no result on the older page."""
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _user("go"),
        _assistant([_tool_use("Bash", {"command": "ls"}, "t1")], "m1"),
        _assistant([{"type": "text", "text": "meanwhile"}], "m2"),
        _user([_tool_result("t1", "a b c")]),
    ])
    newest = st.transcript_page(path, limit=1)
    assert _kinds(newest["entries"]) == ["assistant", "tool_result"]
    assert newest["entries"][1]["text"] == "a b c"
    older = st.transcript_page(path, before=newest["next_cursor"], limit=1)
    assert _kinds(older["entries"]) == ["user", "tool_call"]
    assert older["entries"][1]["result"] is None
    assert older["next_cursor"] is None


def test_missing_file_raises_oserror(tmp_path: Path):
    with pytest.raises(OSError):
        st.transcript_page(tmp_path / "nope.jsonl")


def test_empty_file_is_an_empty_page(tmp_path: Path):
    path = tmp_path / "empty.jsonl"
    path.write_bytes(b"")
    assert st.transcript_page(path) == {"entries": [], "next_cursor": None}


def test_page_exposes_offset_on_turns_only(tmp_path: Path):
    """#985: a turn keeps its byte offset (the copy-full-text route's
    lookup key) — the folded kinds still have it stripped, unchanged."""
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _user("hi"),
        _assistant([{"type": "thinking", "thinking": "hmm"}], "m1"),
        _assistant([{"type": "text", "text": "hello"}], "m1"),
    ])
    page = st.transcript_page(path)
    by_kind = {e["kind"]: e for e in page["entries"]}
    assert isinstance(by_kind["user"]["offset"], int)
    assert isinstance(by_kind["assistant"]["offset"], int)
    assert "offset" not in by_kind["thinking"]


# ---------------------------------------------------------- entry_full_text


def test_entry_full_text_user_is_uncapped(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(st, "USER_TEXT_CAP", 5)
    path = _write_jsonl(tmp_path / "t.jsonl", [_user("a long prompt that would be capped")])
    page = st.transcript_page(path)
    entry = page["entries"][0]
    assert entry["truncated"] is True
    full = st.entry_full_text(path, entry["offset"], "claude")
    assert full == {"text": "a long prompt that would be capped", "truncated": False}


def test_entry_full_text_merges_assistant_blocks_across_lines(tmp_path: Path, monkeypatch):
    """The page-serving reader caps an assistant reply at 20 chars here; the
    full-text route reconstructs both merged text blocks — with a tool call
    and its result (different lines, different kinds) sitting between them,
    same shape :func:`_drop_partial_leading_message` documents."""
    monkeypatch.setattr(st, "ASSISTANT_TEXT_CAP", 20)
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _assistant([{"type": "text", "text": "x" * 15}], "m1"),
        _assistant([_tool_use("Bash", {"command": "ls"}, "t1")], "m1"),
        _user([_tool_result("t1", "listing")]),
        _assistant([{"type": "text", "text": "y" * 15}], "m1"),
    ])
    page = st.transcript_page(path)
    entry = next(e for e in page["entries"] if e["kind"] == "assistant")
    assert entry["truncated"] is True and len(entry["text"]) == 20
    full = st.entry_full_text(path, entry["offset"], "claude")
    assert full == {"text": "x" * 15 + "\n\n" + "y" * 15, "truncated": False}


def test_entry_full_text_codex_assistant(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(st, "ASSISTANT_TEXT_CAP", 5)
    path = _write_jsonl(tmp_path / "rollout.jsonl", [
        _codex({"type": "message", "role": "assistant",
               "content": [{"type": "output_text", "text": "a whole answer"}]}),
    ])
    page = st.transcript_page(path, flavor="codex")
    entry = page["entries"][0]
    assert entry["truncated"] is True
    full = st.entry_full_text(path, entry["offset"], "codex")
    assert full == {"text": "a whole answer", "truncated": False}


def test_entry_full_text_unknown_offset_is_none(tmp_path: Path):
    path = _write_jsonl(tmp_path / "t.jsonl", [_user("hi")])
    assert st.entry_full_text(path, 3, "claude") is None       # mid-line, no turn starts there
    assert st.entry_full_text(path, 9_999, "claude") is None   # past EOF


# --------------------------------------------------------- codex_entries


def _codex(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "response_item", "timestamp": "2026-09-08T15:49:10Z", "payload": payload}


def test_codex_entries_grammar(tmp_path: Path):
    path = _write_jsonl(tmp_path / "rollout.jsonl", [
        {"type": "session_meta", "payload": {"cwd": "E:/x"}},
        _codex({"type": "message", "role": "developer",
                "content": [{"type": "input_text", "text": "<skills_instructions>…"}]}),
        _codex({"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "<environment_context>cwd</environment_context>"}]}),
        _codex({"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "review #877"}]}),
        _codex({"type": "reasoning", "summary": [{"type": "summary_text", "text": "plan it"}]}),
        _codex({"type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "Inspecting."}]}),
        _codex({"type": "custom_tool_call", "call_id": "c1", "name": "exec", "input": "Get-Content x"}),
        _codex({"type": "custom_tool_call_output", "call_id": "c1",
                "output": [{"type": "input_text", "text": "done"}]}),
        _codex({"type": "function_call", "call_id": "c2", "name": "shell", "arguments": "{\"cmd\": \"ls\"}"}),
        _codex({"type": "function_call_output", "call_id": "c2", "output": "a\nb"}),
        _codex({"type": "agent_message", "author": "/root", "content": [{"type": "input_text", "text": "NEW_TASK"}]}),
        {"type": "event_msg", "payload": {"type": "token_count"}},
    ])
    entries = _parse_whole(path, st.codex_entries)
    assert _kinds(entries) == ["system", "system", "user", "thinking", "assistant",
                               "tool_call", "tool_call", "system"]
    assert [e["label"] for e in entries if e["kind"] == "system"] == ["developer", "system", "sub-agent"]
    assert entries[2]["text"] == "review #877"
    assert entries[5]["name"] == "exec" and entries[5]["result"] == "done"
    assert entries[6]["name"] == "shell" and entries[6]["result"] == "a\nb"
    assert entries[7]["sidechain"] is True


def test_codex_pages_concatenate(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(st, "WINDOW_BYTES", 250)
    lines = []
    for i in range(30):
        lines.append(_codex({"type": "message", "role": "user",
                             "content": [{"type": "input_text", "text": f"ask {i}"}]}))
        lines.append(_codex({"type": "custom_tool_call", "call_id": f"c{i}", "name": "exec", "input": f"run {i}"}))
        lines.append(_codex({"type": "custom_tool_call_output", "call_id": f"c{i}",
                             "output": [{"type": "input_text", "text": "ok" * (i % 9)}]}))
        lines.append(_codex({"type": "message", "role": "assistant",
                             "content": [{"type": "output_text", "text": f"answer {i}"}]}))
    path = _write_jsonl(tmp_path / "rollout.jsonl", lines)
    pages = _walk(path, 5, flavor="codex")
    assert len(pages) > 1
    assert [e for page in reversed(pages) for e in page] == _parse_whole(path, st.codex_entries)


# ---------------------------------------------------------- grok_entries

# The captured Grok Build session (#1012): two probe prompts were text-only,
# so this one was recorded specifically to pin the two shapes they missed —
# a read-only tool call with its result, and a long multi-paragraph reply.
# Redacted (home paths as `~`), never hand-written.
GROK_CAPTURE = Path(__file__).parent / "fixtures" / "grok_updates.jsonl"


def _grok(update: Dict[str, Any], *, ts: int = 1_789_670_620, **meta: Any) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "timestamp": ts, "method": "session/update",
        "params": {"sessionId": "s-1", "update": update, "_meta": dict(meta)},
    }
    return row


def _grok_chunk(kind: str, text: str, **kw: Any) -> Dict[str, Any]:
    return _grok({"sessionUpdate": kind, "content": {"type": "text", "text": text}}, **kw)


def test_grok_entries_grammar_from_capture():
    """The real capture, parsed whole: the grammar and the turn order."""
    entries = _parse_whole(GROK_CAPTURE, st.grok_entries)
    assert _kinds(entries) == [
        "user", "thinking", "assistant", "tool_call", "thinking", "assistant"
    ]
    assert entries[0]["text"].startswith("Use exactly one read-only tool call")
    # The tool call: name from `title`, summary from `rawInput`, and the
    # result text recovered although this tool emitted no ACP `content`.
    assert entries[3]["name"] == "list_dir"
    assert entries[3]["summary"].endswith("docs")
    assert "architecture.mmd" in entries[3]["result"]
    # `hook_execution` and `turn_completed` carry no text and are dropped.
    assert len(entries) == 6


def test_grok_message_runs_either_side_of_a_tool_stay_separate():
    """The finding that decided the coalescing rule (#1012).

    One turn wrote two ``agent_message_chunk`` lines: a preamble *before*
    the tool call and the real answer *after* it. Merging a turn's chunks
    would hoist the answer above the tool call that produced it, so only
    consecutive chunks merge — these two must stay two entries, in order.
    """
    entries = _parse_whole(GROK_CAPTURE, st.grok_entries)
    assistants = [e for e in entries if e["kind"] == "assistant"]
    assert len(assistants) == 2
    assert assistants[0]["text"].startswith("I'll list the `docs` folder")
    assert assistants[1]["text"].startswith("The `docs` folder")
    # …and the tool call sits between them, not after both.
    assert _kinds(entries).index("tool_call") == 3


def test_grok_long_reply_arrives_as_one_chunk():
    """Grok folds the stream itself: a three-paragraph reply is one line.

    This is what the #989 probe recorded as unknown. Measured here, not
    assumed — `chunkId` counts the stream chunks Grok merged before
    writing, so a high count on a single line is the evidence.
    """
    rows = [json.loads(line) for line in GROK_CAPTURE.read_text(encoding="utf-8").splitlines() if line.strip()]
    replies = [r for r in rows
               if r["params"]["update"].get("sessionUpdate") == "agent_message_chunk"]
    answer = replies[-1]
    text = answer["params"]["update"]["content"]["text"]
    assert text.count("\n\n") >= 2 and len(text) > 2_000   # three paragraphs, one line
    assert answer["params"]["_meta"]["chunkId"] > 100      # …folded from many stream chunks


def test_grok_consecutive_chunks_of_one_kind_coalesce(tmp_path: Path):
    """The safety net: adjacent same-kind chunks merge, keeping the first
    offset so a page cursor can never split a coalesced turn."""
    path = _write_jsonl(tmp_path / "updates.jsonl", [
        _grok_chunk("user_message_chunk", "ask"),
        _grok_chunk("agent_message_chunk", "part one"),
        _grok_chunk("agent_message_chunk", "part two"),
    ])
    entries = _parse_whole(path, st.grok_entries)
    assert _kinds(entries) == ["user", "assistant"]
    assert entries[1]["text"] == "part one\n\npart two"
    # The *first* chunk's offset — the byte the cursor contract hands back —
    # not the second's, or a cursor could land inside the coalesced turn.
    first_reply_offset = len(path.read_bytes().split(b"\n")[0]) + 1
    assert entries[1]["offset"] == first_reply_offset


def test_grok_timestamps_are_iso_not_raw_epoch(tmp_path: Path):
    """Grok stamps in epoch seconds/ms; the client renders with
    ``new Date(ts)``, which reads a bare number as milliseconds — a raw
    epoch-seconds value would show 1970. Every flavour emits ISO."""
    path = _write_jsonl(tmp_path / "updates.jsonl", [
        _grok_chunk("user_message_chunk", "ask", agentTimestampMs=1_789_670_620_500),
        _grok_chunk("agent_message_chunk", "reply", ts=1_789_670_621),
    ])
    entries = _parse_whole(path, st.grok_entries)
    assert entries[0]["timestamp"] == "2026-09-17T18:43:40.500000Z"   # ms wins
    assert entries[1]["timestamp"] == "2026-09-17T18:43:41Z"          # seconds fallback
    # A record with no usable stamp says so rather than inventing one.
    bare = _write_jsonl(tmp_path / "bare.jsonl", [
        {"method": "session/update",
         "params": {"update": {"sessionUpdate": "user_message_chunk",
                               "content": {"type": "text", "text": "x"}}}},
    ])
    assert _parse_whole(bare, st.grok_entries)[0]["timestamp"] is None


def test_grok_tool_result_sources_and_textless_completion(tmp_path: Path):
    """Three measured result shapes, in the order the reader tries them."""
    path = _write_jsonl(tmp_path / "updates.jsonl", [
        _grok({"sessionUpdate": "tool_call", "toolCallId": "t1",
               "title": "read_file", "rawInput": {"target_file": "~/x.md"}}),
        # 1. ACP content blocks (ReadFile, GrepSearch carried these).
        _grok({"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed",
               "content": [{"type": "content", "content": {"type": "text", "text": "file body"}}]}),
        _grok({"sessionUpdate": "tool_call", "toolCallId": "t2",
               "title": "list_dir", "rawInput": {"target_directory": "~/docs"}}),
        # 2. No content blocks — reach one level into `rawOutput` (ListDir).
        _grok({"sessionUpdate": "tool_call_update", "toolCallId": "t2", "status": "completed",
               "rawOutput": {"type": "ListDir", "Content": {"content": "- a.md\n- b.md"}}}),
        _grok({"sessionUpdate": "tool_call", "toolCallId": "t3",
               "title": "scheduler_list", "rawInput": {}}),
        # 3. Neither — say which output type it was, never an empty result.
        _grok({"sessionUpdate": "tool_call_update", "toolCallId": "t3", "status": "completed",
               "rawOutput": {"type": "SchedulerList", "tasks": []}}),
    ])
    entries = _parse_whole(path, st.grok_entries)
    assert _kinds(entries) == ["tool_call", "tool_call", "tool_call"]
    assert entries[0]["result"] == "file body"
    assert entries[1]["result"] == "- a.md\n- b.md"
    assert entries[2]["result"] == "(SchedulerList: no text output)"


def test_grok_status_less_tool_update_is_not_a_result(tmp_path: Path):
    """Each call gets two ``tool_call_update`` records — a metadata one
    that only restates the call, then the completion. Only the second is a
    result, or every call would show its own arguments as its output."""
    path = _write_jsonl(tmp_path / "updates.jsonl", [
        _grok({"sessionUpdate": "tool_call", "toolCallId": "t1",
               "title": "list_dir", "rawInput": {"target_directory": "~/docs"}}),
        _grok({"sessionUpdate": "tool_call_update", "toolCallId": "t1", "kind": "other",
               "title": "List `~/docs`", "locations": [{"path": "~/docs"}],
               "rawInput": {"variant": "ListDir"}}),
    ])
    entries = _parse_whole(path, st.grok_entries)
    assert len(entries) == 1 and entries[0]["result"] is None


def _grok_conversation(turns: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for i in range(turns):
        rows.append(_grok_chunk("user_message_chunk", f"ask {i}"))
        rows.append(_grok_chunk("agent_thought_chunk", f"thinking about {i} " * 3))
        rows.append(_grok({"sessionUpdate": "tool_call", "toolCallId": f"t{i}",
                           "title": "grep", "rawInput": {"pattern": f"p{i}"}}))
        rows.append(_grok({"sessionUpdate": "tool_call_update", "toolCallId": f"t{i}",
                           "status": "completed",
                           "content": [{"type": "content",
                                        "content": {"type": "text", "text": "hit " * (i % 7)}}]}))
        rows.append(_grok_chunk("agent_message_chunk", f"answer {i}"))
        rows.append(_grok({"sessionUpdate": "turn_completed", "stop_reason": "end_turn"}))
    return rows


def test_grok_pages_concatenate_across_window_boundaries(tmp_path: Path, monkeypatch):
    """Every page, oldest-first, equals one whole parse — nothing split or
    duplicated when the window edge lands mid-turn."""
    monkeypatch.setattr(st, "WINDOW_BYTES", 400)
    path = _write_jsonl(tmp_path / "updates.jsonl", _grok_conversation(20))
    pages = _walk(path, 4, flavor="grok")
    assert len(pages) > 1
    assert [e for page in reversed(pages) for e in page] == _parse_whole(path, st.grok_entries)


def test_grok_entry_full_text_is_uncapped(tmp_path: Path, monkeypatch):
    """A truncated turn reads back whole — what Chat's copy (#985) and
    read-aloud (#988) call when a page entry came back ``truncated``."""
    monkeypatch.setattr(st, "ASSISTANT_TEXT_CAP", 40)
    long_reply = "paragraph one. " * 40
    path = _write_jsonl(tmp_path / "updates.jsonl", [
        _grok_chunk("user_message_chunk", "ask"),
        _grok_chunk("agent_message_chunk", long_reply),
    ])
    page = st.transcript_page(path, flavor="grok")
    reply = [e for e in page["entries"] if e["kind"] == "assistant"][0]
    assert reply["truncated"] is True and len(reply["text"]) == 40
    full = st.entry_full_text(path, reply["offset"], "grok")
    assert full["text"] == long_reply.strip() and full["truncated"] is False
    assert st.entry_full_text(path, 3, "grok") is None       # mid-line
    assert st.entry_full_text(path, 99_999, "grok") is None  # past EOF


# --------------------------------------------------------------- router


def test_transcript_path_classified_passkey():
    from app.webapp.middleware import _terminal_guard_level
    assert _terminal_guard_level("/api/claude-code/sessions/abc/transcript") == "passkey"


def test_transcript_refused_off_tailnet(webapp_client):
    client, _, _ = webapp_client
    assert client.get("/api/claude-code/sessions/s1/transcript").status_code == 403


@pytest.fixture
def _bypass_gate(monkeypatch):
    from app.webapp import middleware
    monkeypatch.setattr(
        middleware, "LOOPBACK_HOSTS",
        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
    )


def _live(sid: str = "s1", **extra: Any) -> Dict[str, Any]:
    row = {"session_id": sid, "kind": "pty", "agent": "claude", "alive": True,
           "project_dir": "E:/automation/proj", "started_at": 1_789_000_000}
    row.update(extra)
    return row


class TestTranscriptEndpoint:

    def test_unknown_session(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = []
        body = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert body["available"] is False and body["reason"] == "session_not_found"
        assert body["entries"] == [] and body["next_cursor"] is None

    def test_detached_claude_row_reads_its_claimed_history(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path
    ):
        # #966: a detached row has no PTY capture, but the reader never uses one.
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(kind="remote")]
        path = _write_jsonl(tmp_path / "t.jsonl", _conversation(2))
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: {"transcript_path": str(path)})
        body = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert body["available"] is True and body["source"] == "native"
        assert "prompt 1 p" in [e["text"] for e in body["entries"] if e["kind"] == "user"]

    def test_detached_codex_row_reads_its_rollout(self, webapp_client, _bypass_gate, monkeypatch, tmp_path):
        from app.webapp.routers import session_transcript as router_mod
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(kind="remote", agent="codex")]
        path = _write_jsonl(tmp_path / "rollout.jsonl", [
            _codex({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hey"}]}),
        ])
        monkeypatch.setattr(router_mod, "_find_codex_transcript", lambda session: path)
        body = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert body["available"] is True and body["source"] == "codex"
        monkeypatch.setattr(router_mod, "_find_codex_transcript", lambda session: None)
        assert client.get("/api/claude-code/sessions/s1/transcript").json()["reason"] == "no_transcript"

    def test_grok_row_reads_its_updates_stream(self, webapp_client, _bypass_gate, monkeypatch):
        """A Grok session resolves through the state row's `transcript_path`
        — the same non-Codex path Claude uses — and reports its own source
        name rather than being mislabelled as the Codex reader."""
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(agent="grok")]
        monkeypatch.setattr(
            board, "state_row_for_session",
            lambda live, rows, sid: {"transcript_path": str(GROK_CAPTURE)},
        )
        body = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert body["available"] is True and body["source"] == "grok"
        assert [e["kind"] for e in body["entries"]][:3] == ["user", "thinking", "assistant"]
        entry = [e for e in body["entries"] if e["kind"] == "assistant"][-1]
        full = client.get(
            f"/api/claude-code/sessions/s1/transcript/entry?offset={entry['offset']}"
        ).json()
        assert full["available"] is True
        assert full["text"].startswith("The `docs` folder")

    def test_grok_unclaimed_row_is_no_transcript_not_a_stale_neighbour(
        self, webapp_client, _bypass_gate, monkeypatch
    ):
        """Correlation fails safe (#1012): Grok keeps a session folder per
        working directory, including ones that no longer exist (a removed
        worktree). When the claim walk assigns this session no row, the
        answer is `no_transcript` — never a nearby folder's stream, which
        would show another session's text.
        """
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(agent="grok")]
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: None)
        body = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert body["available"] is False and body["reason"] == "no_transcript"
        assert body["entries"] == []

    def test_detached_unsupported_agent(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(kind="remote", agent="pi")]
        assert client.get("/api/claude-code/sessions/s1/transcript").json()["reason"] == "unsupported_agent"

    def test_unsupported_agent(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(agent="pi")]
        assert client.get("/api/claude-code/sessions/s1/transcript").json()["reason"] == "unsupported_agent"

    def test_no_transcript_when_no_row_or_no_file(self, webapp_client, _bypass_gate, monkeypatch, tmp_path):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live()]
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: None)
        assert client.get("/api/claude-code/sessions/s1/transcript").json()["reason"] == "no_transcript"
        monkeypatch.setattr(
            board, "state_row_for_session",
            lambda live, rows, sid: {"transcript_path": str(tmp_path / "gone.jsonl")},
        )
        assert client.get("/api/claude-code/sessions/s1/transcript").json()["reason"] == "no_transcript"

    def test_read_failure_is_distinct_from_missing(self, webapp_client, _bypass_gate, monkeypatch, tmp_path):
        from app.webapp.routers import session_transcript as router_mod
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live()]
        path = _write_jsonl(tmp_path / "t.jsonl", [_user("hi")])
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: {"transcript_path": str(path)})

        def boom(*args, **kwargs):
            raise PermissionError("locked")

        monkeypatch.setattr(router_mod, "transcript_page", boom)
        assert client.get("/api/claude-code/sessions/s1/transcript").json()["reason"] == "read_failed"

    def test_happy_path_pages_and_never_logs_bodies(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path, caplog
    ):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live()]
        path = _write_jsonl(tmp_path / "t.jsonl", _conversation(6, tool_calls_per_turn=1))
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: {"transcript_path": str(path)})

        with caplog.at_level(logging.INFO):
            first = client.get("/api/claude-code/sessions/s1/transcript?limit=4").json()
        assert first["available"] is True and first["source"] == "native"
        assert first["session_id"] == "s1" and first["reason"] is None
        assert [e["text"] for e in first["entries"] if e["kind"] == "user"] == ["prompt 4 pppp", "prompt 5 ppppp"]
        assert isinstance(first["next_cursor"], int)
        older = client.get(
            f"/api/claude-code/sessions/s1/transcript?limit=4&before={first['next_cursor']}"
        ).json()
        assert [e["text"] for e in older["entries"] if e["kind"] == "user"] == ["prompt 2 pp", "prompt 3 ppp"]
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "prompt 5" not in joined and "reply 5" not in joined
        assert "transcript s1" in joined  # the breadcrumb itself is there

    def test_codex_source(self, webapp_client, _bypass_gate, monkeypatch, tmp_path):
        from app.webapp.routers import session_transcript as router_mod
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(agent="codex")]
        path = _write_jsonl(tmp_path / "rollout.jsonl", [
            _codex({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hey"}]}),
            _codex({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "yo"}]}),
        ])
        monkeypatch.setattr(router_mod, "_find_codex_transcript", lambda session: path)
        body = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert body["source"] == "codex" and _kinds(body["entries"]) == ["user", "assistant"]

    def test_limit_is_validated(self, webapp_client, _bypass_gate, monkeypatch):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live()]
        assert client.get("/api/claude-code/sessions/s1/transcript?limit=0").status_code == 422
        assert client.get("/api/claude-code/sessions/s1/transcript?limit=101").status_code == 422
        assert client.get("/api/claude-code/sessions/s1/transcript?before=-1").status_code == 422


def test_transcript_entry_path_classified_passkey():
    """#985: a distinct guard-table row — '.../transcript/entry' does not
    end with '/transcript', so the sibling route's rule does not cover it
    for free (the exact omission class #997 found on the send route)."""
    from app.webapp.middleware import _terminal_guard_level
    assert _terminal_guard_level("/api/claude-code/sessions/abc/transcript/entry") == "passkey"
    assert _terminal_guard_level("/api/claude-code/sessions/abc/transcript") == "passkey"


def test_transcript_entry_refused_off_tailnet(webapp_client):
    client, _, _ = webapp_client
    assert client.get("/api/claude-code/sessions/s1/transcript/entry?offset=0").status_code == 403


class TestTranscriptEntryEndpoint:

    def test_offset_is_required(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live()]
        assert client.get("/api/claude-code/sessions/s1/transcript/entry").status_code == 422
        assert client.get("/api/claude-code/sessions/s1/transcript/entry?offset=-1").status_code == 422

    def test_unknown_session_shares_the_page_routes_reasons(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = []
        body = client.get("/api/claude-code/sessions/s1/transcript/entry?offset=0").json()
        assert body["available"] is False and body["reason"] == "session_not_found"

    def test_happy_path_returns_uncapped_text_and_never_logs_body(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path, caplog
    ):
        monkeypatch.setattr(st, "USER_TEXT_CAP", 5)
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live()]
        path = _write_jsonl(tmp_path / "t.jsonl", [_user("a much longer prompt than the cap")])
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: {"transcript_path": str(path)})
        page = client.get("/api/claude-code/sessions/s1/transcript").json()
        entry = page["entries"][0]
        assert entry["truncated"] is True

        with caplog.at_level(logging.INFO):
            body = client.get(
                f"/api/claude-code/sessions/s1/transcript/entry?offset={entry['offset']}"
            ).json()
        assert body == {
            "available": True, "reason": None, "session_id": "s1",
            "text": "a much longer prompt than the cap", "truncated": False,
        }
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "a much longer prompt" not in joined
        assert "transcript entry s1" in joined

    def test_stale_offset_is_entry_not_found(self, webapp_client, _bypass_gate, monkeypatch, tmp_path):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live()]
        path = _write_jsonl(tmp_path / "t.jsonl", [_user("hi")])
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: {"transcript_path": str(path)})
        body = client.get("/api/claude-code/sessions/s1/transcript/entry?offset=9999").json()
        assert body["available"] is False and body["reason"] == "entry_not_found"

    def test_read_failure_is_distinct_from_entry_not_found(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path
    ):
        from app.webapp.routers import session_transcript as router_mod
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live()]
        path = _write_jsonl(tmp_path / "t.jsonl", [_user("hi")])
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: {"transcript_path": str(path)})

        def boom(*args, **kwargs):
            raise PermissionError("locked")

        monkeypatch.setattr(router_mod, "entry_full_text", boom)
        body = client.get("/api/claude-code/sessions/s1/transcript/entry?offset=0").json()
        assert body["available"] is False and body["reason"] == "read_failed"
