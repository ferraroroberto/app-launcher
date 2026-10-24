"""Coding-tab transcript: the Copilot entry grammar and its paging."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from src import session_transcript as st
from src.transcript_flavors import _shared as tf_shared

from tests.unit.transcript._support import COPILOT_CAPTURE, _kinds, _lines_of, _parse_whole, _walk, _write_jsonl


def _cop(kind: str, data: Dict[str, Any], ts: str = "2026-09-17T21:58:00.000Z",
         **extra: Any) -> Dict[str, Any]:
    row = {"type": kind, "data": data, "id": "e1", "parentId": None, "timestamp": ts}
    row.update(extra)
    return row


def test_copilot_entries_grammar_from_capture():
    """The real capture, parsed whole: the grammar and the turn order.

    Pins what the issue could not know without a capture — the tool records
    are their own ``tool.execution_*`` event pair carrying an exact
    ``toolCallId``, not the ``toolRequests`` array the issue pointed at, and
    a tool-calling ``assistant.message`` has no text of its own.
    """
    entries = _parse_whole(COPILOT_CAPTURE, st.copilot_entries)
    assert _kinds(entries) == [
        "system",                                     # the refused --model flag
        "user", "tool_call", "assistant",
        "user", "thinking", "tool_call", "assistant",
        "user", "thinking", "assistant",
        "user", "tool_call", "assistant",         # the failing view (#1020)
    ]
    # The launcher's model flag is rejected at startup (#1017) — folded, not
    # dropped, so the reason is visible rather than mysterious.
    assert entries[0]["label"] == "model"
    assert "is not available" in entries[0]["text"]
    assert entries[1]["text"].startswith("Run the shell command: git rev-parse")
    # The call: name and arguments come from `tool.execution_start`, and the
    # result is paired back by its exact id.
    assert entries[2]["name"] == "powershell"
    assert entries[2]["summary"] == "git rev-parse --short HEAD"
    assert entries[2]["action"] == {"verb": "ran", "command": "git rev-parse --short HEAD"}
    assert entries[2]["result"].startswith("d9580a3")
    assert entries[3]["text"] == "d9580a3"
    # A tool call always precedes the reply that reports it: the call is
    # written on a later line than the assistant message that requested it,
    # so no answer is ever hoisted above its own call (#1012's trap). Read
    # from the raw builder — `_page` strips `offset` off the folded kinds.
    raw = st.copilot_entries(_lines_of(COPILOT_CAPTURE))
    call, reply = raw[2], raw[3]
    assert call["kind"] == "tool_call" and reply["kind"] == "assistant"
    assert call["offset"] < reply["offset"]
    # Copilot's two outcome fields, at two levels — both needed (#1020).
    # A command that exits 1 is still `success: true` (the *tool* ran fine),
    # so the failure is read from `shellExecution.exitCode`. The exit-code
    # footer stays in the card text too.
    assert entries[6]["summary"] == "git rev-parse --short NO_SUCH_REF_xyz"
    assert "exit code 1" in entries[6]["result"]
    assert entries[6]["error"] is True
    assert "error" not in entries[2]           # exitCode 0, same capture
    assert entries[7]["text"] == "fatal: Needed a single revision"
    assert entries[10]["text"].count("\n\n") == 2       # the multi-paragraph reply
    # A *tool*-level failure is the other shape: `success: false` with an
    # `error` object and **no `result` key at all** — so the card's text has
    # to come from `error.message`, or a failed call renders blank.
    assert entries[12]["name"] == "view"
    assert entries[12]["error"] is True
    assert entries[12]["result"] == "Path does not exist"


def test_copilot_plumbing_never_reaches_a_card():
    """The three families that would wreck the view if a reader guessed.

    Each marker is a real record in the capture, so this fails the moment
    the parser starts matching loosely rather than on exact event types.
    """
    entries = _parse_whole(COPILOT_CAPTURE, st.copilot_entries)
    blob = json.dumps(entries)
    # 1. The system prompt: one ~87 KB line in the real log.
    assert "SYSTEM_PROMPT_MUST_NEVER_RENDER" not in blob
    # 2. `model.*` bookkeeping re-serialises the whole conversation on every
    #    model call — rendering it would multiply every turn.
    assert "DUPLICATE_MUST_NEVER_RENDER" not in blob
    # 3. `encryptedContent` / `reasoningOpaque` / `reasoningBlocks` are
    #    provider blobs; a card must never show a screenful of base64.
    assert "OPAQUE_MUST_NEVER_RENDER" not in blob
    # Exactly one tool card per call — `toolRequests` on the assistant
    # message is the same call again and must not double it.
    assert _kinds(entries).count("tool_call") == 3


def test_copilot_user_prompt_is_the_typed_text_not_the_wrapped_one(tmp_path: Path):
    """``content`` is what the user typed; ``transformedContent`` is the same
    text wrapped in a ``<current_datetime>`` block for the model."""
    path = _write_jsonl(tmp_path / "cop.jsonl", [
        _cop("user.message", {
            "content": "say hi",
            "transformedContent": "<current_datetime>2026-09-17T19:24:03+02:00"
                                  "</current_datetime>\n\nsay hi",
        }),
    ])
    entries = _parse_whole(path, st.copilot_entries)
    assert [e["text"] for e in entries] == ["say hi"]


def test_copilot_tool_results_pair_by_id_and_orphans_stand_alone(tmp_path: Path):
    """``toolCallId`` is exact, so interleaved calls pair correctly and a
    result whose call fell off the page stands alone rather than mis-pairing.
    """
    path = _write_jsonl(tmp_path / "cop.jsonl", [
        _cop("tool.execution_start", {"toolCallId": "a", "toolName": "view",
                                      "arguments": {"path": "one.txt"}}),
        _cop("tool.execution_start", {"toolCallId": "b", "toolName": "view",
                                      "arguments": {"path": "two.txt"}}),
        # Answered out of order: the ids, not the order, decide the pairing.
        _cop("tool.execution_complete", {"toolCallId": "b", "success": True,
                                         "result": {"content": "two body"}}),
        _cop("tool.execution_complete", {"toolCallId": "a", "success": True,
                                         "result": {"content": "one body"}}),
        _cop("tool.execution_complete", {"toolCallId": "gone", "success": True,
                                         "result": {"content": "orphan body"}}),
    ])
    entries = _parse_whole(path, st.copilot_entries)
    assert _kinds(entries) == ["tool_call", "tool_call", "tool_result"]
    assert entries[0]["summary"] == "one.txt" and entries[0]["result"] == "one body"
    assert entries[1]["summary"] == "two.txt" and entries[1]["result"] == "two body"
    assert entries[2]["text"] == "orphan body" and entries[2]["tool_use_id"] == "gone"


def test_copilot_result_prefers_content_over_detailed_and_placeholders_binary(
    tmp_path: Path,
):
    """``content`` and ``detailedContent`` are different views, not a
    short/long pair: on the three completions here where they differ,
    ``content`` was the tool output the model saw. A non-textual result
    becomes a placeholder instead of a dump."""
    path = _write_jsonl(tmp_path / "cop.jsonl", [
        _cop("tool.execution_start", {"toolCallId": "a", "toolName": "view",
                                      "arguments": {"path": "f"}}),
        _cop("tool.execution_complete", {"toolCallId": "a", "success": True,
                                         "result": {"content": "the file body",
                                                    "detailedContent": "a diff of it"}}),
        _cop("tool.execution_start", {"toolCallId": "b", "toolName": "view",
                                      "arguments": {"path": "g"}}),
        _cop("tool.execution_complete", {"toolCallId": "b", "success": True,
                                         "result": {"detailedContent": "only detailed"}}),
        _cop("tool.execution_start", {"toolCallId": "c", "toolName": "screenshot",
                                      "arguments": {"path": "h"}}),
        _cop("tool.execution_complete", {"toolCallId": "c", "success": True,
                                         "result": {"image": {"bytes": 1}}}),
    ])
    entries = _parse_whole(path, st.copilot_entries)
    assert entries[0]["result"] == "the file body"
    assert entries[1]["result"] == "only detailed"          # fallback
    assert entries[2]["result"] == "[non-text result]"      # never a repr dump


def test_copilot_readable_thinking_folds_and_opaque_reasoning_does_not(tmp_path: Path):
    """``reasoningText`` is prose and folds as ``thinking``; the three
    encrypted neighbours are never read at all."""
    path = _write_jsonl(tmp_path / "cop.jsonl", [
        _cop("assistant.message", {
            "content": "the answer",
            "reasoningText": "  weighing the options  ",
            "reasoningOpaque": "AAAABBBBCCCC",
            "encryptedContent": "DDDDEEEEFFFF",
            "reasoningBlocks": {"blocks": [{"encrypted_content": "GGGGHHHH"}]},
            "apiCallId": "IIIIJJJJ",
        }),
    ])
    entries = _parse_whole(path, st.copilot_entries)
    assert _kinds(entries) == ["thinking", "assistant"]
    assert entries[0]["text"] == "weighing the options"
    blob = json.dumps(entries)
    for opaque in ("AAAABBBB", "DDDDEEEE", "GGGGHHHH", "IIIIJJJJ"):
        assert opaque not in blob


def test_copilot_timestamps_are_the_envelope_iso_string(tmp_path: Path):
    """ISO-8601 on the event envelope, passed through untouched — the
    client's ``new Date`` reads it, so no turn renders as 1970 (#1012)."""
    path = _write_jsonl(tmp_path / "cop.jsonl", [
        _cop("user.message", {"content": "hi"}, ts="2026-09-17T21:58:01.005Z"),
    ])
    entries = _parse_whole(path, st.copilot_entries)
    assert entries[0]["timestamp"] == "2026-09-17T21:58:01.005Z"


def _copilot_conversation(turns: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for i in range(turns):
        rows.append(_cop("user.message", {"content": f"prompt {i} " + "p" * i}))
        # The real log's dominant weight: bookkeeping between every turn.
        rows.append(_cop("model.messages_snapshot",
                         {"messages": [{"role": "user", "content": "noise " * 8}]}))
        rows.append(_cop("assistant.message",
                         {"content": "", "toolRequests": [{"toolCallId": f"c{i}"}]}))
        rows.append(_cop("tool.execution_start", {"toolCallId": f"c{i}",
                                                  "toolName": "powershell",
                                                  "arguments": {"command": f"cmd {i}"}}))
        rows.append(_cop("tool.execution_complete", {"toolCallId": f"c{i}",
                                                     "success": True,
                                                     "result": {"content": f"out {i}"}}))
        rows.append(_cop("assistant.message", {"content": f"answer {i}",
                                               "phase": "final_answer"}))
    return rows


def test_copilot_pages_concatenate_across_window_boundaries(tmp_path: Path, monkeypatch):
    """Every page, oldest-first, equals one whole parse — nothing split or
    duplicated when the window edge lands mid-turn."""
    monkeypatch.setattr(st, "WINDOW_BYTES", 400)
    path = _write_jsonl(tmp_path / "cop.jsonl", _copilot_conversation(20))
    pages = _walk(path, 4, flavor="copilot")
    assert len(pages) > 1
    assert [e for page in reversed(pages) for e in page] == _parse_whole(
        path, st.copilot_entries
    )


def test_copilot_entry_full_text_is_uncapped(tmp_path: Path, monkeypatch):
    """A truncated turn reads back whole — what Chat's copy (#985) and
    read-aloud (#988) call when a page entry came back ``truncated``."""
    monkeypatch.setattr(tf_shared, "ASSISTANT_TEXT_CAP", 40)
    long_reply = "paragraph one. " * 40
    path = _write_jsonl(tmp_path / "cop.jsonl", [
        _cop("user.message", {"content": "ask"}),
        _cop("assistant.message", {"content": long_reply, "reasoningText": "brief"}),
    ])
    page = st.transcript_page(path, flavor="copilot")
    reply = [e for e in page["entries"] if e["kind"] == "assistant"][0]
    assert reply["truncated"] is True and len(reply["text"]) == 40
    full = st.entry_full_text(path, reply["offset"], "copilot")
    assert full["text"] == long_reply.strip() and full["truncated"] is False
    assert st.entry_full_text(path, 3, "copilot") is None       # mid-line
    assert st.entry_full_text(path, 99_999, "copilot") is None  # past EOF


def test_copilot_huge_plumbing_line_does_not_break_paging(tmp_path: Path, monkeypatch):
    """A single ``system.message`` reached 87 KB in the capture — wider than
    a window once ``WINDOW_BYTES`` is small. The page widens in place rather
    than tearing it, and the turns around it still read whole."""
    monkeypatch.setattr(st, "WINDOW_BYTES", 400)
    path = _write_jsonl(tmp_path / "cop.jsonl", [
        _cop("user.message", {"content": "ask"}),
        _cop("system.message", {"role": "system", "content": "S" * 5_000}),
        _cop("assistant.message", {"content": "answer", "phase": "final_answer"}),
    ])
    page = st.transcript_page(path, flavor="copilot")
    assert _kinds(page["entries"]) == ["user", "assistant"]
    assert page["entries"][1]["text"] == "answer"
