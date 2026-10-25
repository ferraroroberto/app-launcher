"""Coding-tab transcript: Codex and Grok entry grammars and their paging."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from src.session_view import transcript as st
from src.transcript_flavors import _shared as tf_shared

from tests.unit.transcript._support import (
    GROK_CAPTURE,
    _codex,
    _grok,
    _grok_chunk,
    _kinds,
    _parse_whole,
    _walk,
    _write_jsonl,
)


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


def test_grok_entries_grammar_from_capture():
    """The real capture, parsed whole: the grammar and the turn order."""
    entries = _parse_whole(GROK_CAPTURE, st.grok_entries)
    assert _kinds(entries) == [
        "user", "thinking", "assistant", "tool_call", "thinking", "assistant",
        "user", "tool_call", "assistant",          # the failing read (#1020)
    ]
    assert entries[0]["text"].startswith("Use exactly one read-only tool call")
    # The tool call: name from `title`, summary from `rawInput`, and the
    # result text recovered although this tool emitted no ACP `content`.
    assert entries[3]["name"] == "list_dir"
    assert entries[3]["summary"].endswith("docs")
    assert "action" not in entries[3]      # not a command/edit/read tool: rendered as before (#1266)
    assert "architecture.mmd" in entries[3]["result"]
    # `hook_execution` and `turn_completed` carry no text and are dropped.
    assert len(entries) == 9


def test_grok_message_runs_either_side_of_a_tool_stay_separate():
    """The finding that decided the coalescing rule (#1012).

    One turn wrote two ``agent_message_chunk`` lines: a preamble *before*
    the tool call and the real answer *after* it. Merging a turn's chunks
    would hoist the answer above the tool call that produced it, so only
    consecutive chunks merge — these two must stay two entries, in order.
    """
    entries = _parse_whole(GROK_CAPTURE, st.grok_entries)
    # The first turn only — the capture gained a second exchange with #1020's
    # failing read, whose own reply would otherwise be counted here.
    first_turn = entries[:6]
    assistants = [e for e in first_turn if e["kind"] == "assistant"]
    assert len(assistants) == 2
    assert assistants[0]["text"].startswith("I'll list the `docs` folder")
    assert assistants[1]["text"].startswith("The `docs` folder")
    # …and the tool call sits between them, not after both.
    assert _kinds(first_turn).index("tool_call") == 3


def test_grok_long_reply_arrives_as_one_chunk():
    """Grok folds the stream itself: a three-paragraph reply is one line.

    This is what the #989 probe recorded as unknown. Measured here, not
    assumed — `chunkId` counts the stream chunks Grok merged before
    writing, so a high count on a single line is the evidence.
    """
    rows = [json.loads(line) for line in GROK_CAPTURE.read_text(encoding="utf-8").splitlines() if line.strip()]
    replies = [r for r in rows
               if r["params"]["update"].get("sessionUpdate") == "agent_message_chunk"]
    # The long reply is the first turn's answer; the capture's later
    # exchange (#1020's failing read) ends in a one-line reply.
    answer = replies[1]
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


def test_grok_failed_call_keeps_its_result_and_is_marked(tmp_path: Path):
    """The regression this fixes twice over (#1020).

    Grok reports a failed tool call as ``status: "failed"`` — a value the
    reader had never seen, because the 19 ``tool_call_update`` records on
    disk when it was written were all ``completed``. It therefore attached a
    result only on ``completed``, so a *failed* call's result was dropped
    entirely and rendered "no result on this page": the error text the user
    most needed was the one thing the page could not show.
    """
    entries = _parse_whole(GROK_CAPTURE, st.grok_entries)
    failed = entries[7]
    assert failed["kind"] == "tool_call" and failed["name"] == "read_file"
    assert failed["result"].startswith("Error: ")
    assert failed["error"] is True
    # The working call in the same capture stays unmarked.
    assert "error" not in entries[3]


def test_grok_in_flight_status_never_occupies_the_result_slot(tmp_path: Path):
    """``pending``/``in_progress`` are progress, not results. Attaching one
    would fill the call's single result slot and lock the real result out —
    the reason the status set is an explicit pair rather than "anything but
    the status-less half"."""
    path = _write_jsonl(tmp_path / "updates.jsonl", [
        _grok({"sessionUpdate": "tool_call", "toolCallId": "t1",
               "title": "read_file", "rawInput": {"target_file": "~/x.md"}}),
        _grok({"sessionUpdate": "tool_call_update", "toolCallId": "t1",
               "status": "in_progress",
               "content": [{"type": "content",
                            "content": {"type": "text", "text": "reading…"}}]}),
        _grok({"sessionUpdate": "tool_call_update", "toolCallId": "t1",
               "status": "failed",
               "content": [{"type": "content",
                            "content": {"type": "text", "text": "boom"}}]}),
    ])
    entries = _parse_whole(path, st.grok_entries)
    assert len(entries) == 1
    assert entries[0]["result"] == "boom" and entries[0]["error"] is True


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
    monkeypatch.setattr(tf_shared, "ASSISTANT_TEXT_CAP", 40)
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
