"""Coding-tab transcript: the Antigravity entry grammar and its paging."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from src.session_view import transcript as st
from src.transcript_flavors import _shared as tf_shared

from tests.unit.transcript._support import AGY_CAPTURE, _kinds, _parse_whole, _walk, _write_jsonl


def _agy(step: int, source: str, step_type: str, **extra: Any) -> Dict[str, Any]:
    row = {"step_index": step, "source": source, "type": step_type,
           "status": "DONE", "created_at": "2026-09-17T21:08:20Z"}
    row.update(extra)
    return row


def _agy_prompt(text: str, step: int = 0) -> Dict[str, Any]:
    return _agy(step, "USER_EXPLICIT", "USER_INPUT", content=(
        f"<USER_REQUEST>\n{text}\n</USER_REQUEST>\n"
        "<ADDITIONAL_METADATA>\nThe current local time is: 2026-09-17T23:08:20+02:00.\n"
        "</ADDITIONAL_METADATA>"
    ))


def test_antigravity_entries_grammar_from_capture():
    """The real capture, parsed whole: the grammar and the turn order.

    Pins the two shapes the issue could not know without a capture — a tool
    call rides on the ``PLANNER_RESPONSE`` that has no ``content`` of its
    own, and its result is the next ``MODEL`` step whose type does *not*
    name the tool (``list_dir`` answered as ``GENERIC``).
    """
    entries = _parse_whole(AGY_CAPTURE, st.antigravity_entries)
    assert _kinds(entries) == [
        "user", "thinking", "tool_call", "thinking", "assistant",
        "user", "thinking", "tool_call", "assistant",
        "user", "thinking", "assistant",          # the leaked Stop turn (#1016)
        "user", "thinking", "tool_call", "assistant",   # the ERROR step (#1020)
    ]
    assert entries[0]["text"].startswith("List the files in the current folder")
    # The tool call: name from the step's `tool_calls`, summary from its
    # first real argument (not the `toolAction`/`toolSummary` UI labels),
    # result attached from the *following* step.
    assert entries[2]["name"] == "list_dir"
    assert entries[2]["summary"] == r"E:\work\project"
    assert entries[2]["result"].startswith('{"name":".agents"')
    # The harness's own timing header never reaches the card.
    assert "Created At:" not in entries[2]["result"]
    assert entries[4]["text"].count("\n\n") >= 2          # the multi-paragraph reply
    # Antigravity's two failure shapes, the reason this flavour is
    # `partial` (#1020). A shell command exiting non-zero is `status:
    # "DONE"` — indistinguishable from a working call except in English
    # prose, so it is deliberately left unmarked...
    assert entries[7]["name"] == "run_command"
    assert "exited with code 1" in entries[7]["result"]
    assert "error" not in entries[7]
    # ...while a tool that could not run at all is `status: "ERROR"`, which
    # is structural and is marked.
    assert entries[14]["name"] == "view_file"
    assert entries[14]["error"] is True
    assert entries[9]["text"] == "quit"


def test_antigravity_user_prompt_shows_only_the_request():
    """A typed prompt arrives wrapped in ``<USER_REQUEST>`` with the local
    time — and sometimes a settings change — appended after it. Only the
    request is shown; an unwrapped step still renders rather than vanishing.
    """
    entries = st.antigravity_entries([
        (0, json.dumps(_agy(0, "USER_EXPLICIT", "USER_INPUT", content=(
            "<USER_REQUEST>\nsay hi\n</USER_REQUEST>\n"
            "<ADDITIONAL_METADATA>\nThe current local time is: 2026-09-17T23:08:20+02:00.\n"
            "</ADDITIONAL_METADATA>\n<USER_SETTINGS_CHANGE>\nThe user changed setting "
            "`Model Selection` from None to Gemini 3.1 Pro (Low).\n</USER_SETTINGS_CHANGE>"
        )))),
        (1, json.dumps(_agy(1, "USER_EXPLICIT", "USER_INPUT", content="bare prompt"))),
    ])
    assert [e["text"] for e in entries] == ["say hi", "bare prompt"]


def test_antigravity_tool_results_pair_positionally_and_orphans_stand_alone():
    """No call id exists anywhere, so calls and results pair FIFO — and a
    result arriving with nothing outstanding stands on its own rather than
    attaching to an unrelated call. (Six of the 210 logs on this box have a
    dropped step, which is exactly that case.)"""
    rows = [
        _agy(0, "MODEL", "PLANNER_RESPONSE", tool_calls=[
            {"name": "list_dir", "args": {"DirectoryPath": "a"}},
            {"name": "view_file", "args": {"AbsolutePath": "b"}},
        ]),
        _agy(1, "MODEL", "GENERIC", content="Created At: t\nCompleted At: t\nfirst"),
        _agy(2, "MODEL", "VIEW_FILE", content="Created At: t\nCompleted At: t\nsecond"),
        _agy(3, "MODEL", "RUN_COMMAND", content="Created At: t\norphan"),
    ]
    entries = st.antigravity_entries([(i * 10, json.dumps(r)) for i, r in enumerate(rows)])
    assert _kinds(entries) == ["tool_call", "tool_call", "tool_result"]
    assert [e["name"] for e in entries[:2]] == ["list_dir", "view_file"]
    assert entries[0]["result"] == "first" and entries[1]["result"] == "second"
    assert entries[2]["text"] == "orphan"
    # A call left unanswered keeps `result: None` rather than stealing the
    # next turn's result.
    tail = st.antigravity_entries([(0, json.dumps(rows[0]))])
    assert [e["result"] for e in tail] == [None, None]


def test_antigravity_system_steps_fold_and_contentless_ones_vanish():
    """``SYSTEM``-sourced steps are harness plumbing, folded and labelled;
    ``CONVERSATION_HISTORY`` carries no ``content`` and produces nothing, so
    a resumed session does not open with an empty card."""
    rows = [
        _agy(0, "SYSTEM", "CONVERSATION_HISTORY"),
        _agy(1, "SYSTEM", "EPHEMERAL_MESSAGE", content="<EPHEMERAL_MESSAGE>\nreminder"),
        _agy(2, "SYSTEM", "CHECKPOINT", content="# Resuming from a compaction"),
        _agy(3, "SYSTEM", "ERROR_MESSAGE", status="ERROR",
             error="invalid tool call", exit_code=1),
        _agy(4, "SYSTEM", "SOMETHING_NEW", content="a type this reader has not seen"),
    ]
    entries = st.antigravity_entries([(i * 10, json.dumps(r)) for i, r in enumerate(rows)])
    assert _kinds(entries) == ["system"] * 4
    assert [e["label"] for e in entries] == [
        "ephemeral", "compaction", "error", "system",
    ]
    # An ERROR_MESSAGE with no `content` still shows its `error`.
    assert entries[2]["text"] == "invalid tool call"


def test_antigravity_flat_file_truncation_and_double_encoded_args():
    """The fallback ``transcript.jsonl`` cuts long fields itself (naming
    them in ``truncated_fields``) and JSON-encodes each tool argument a
    second time. Both are undone here, or a turn would report itself whole
    when it is not and a tool card would show escaped quotes."""
    rows = [
        _agy(0, "MODEL", "PLANNER_RESPONSE", content="a short reply",
             thinking="a cut thought", truncated_fields=["content", "thinking"]),
        _agy(1, "MODEL", "PLANNER_RESPONSE", tool_calls=[{
            "name": "run_command",
            "args": {"CommandLine": "\"dir C:\\\\tmp\"", "toolSummary": "\"Listing\""},
        }]),
    ]
    entries = st.antigravity_entries([(i * 10, json.dumps(r)) for i, r in enumerate(rows)])
    assert [e["kind"] for e in entries] == ["thinking", "assistant", "tool_call"]
    assert entries[0]["truncated"] is True and entries[1]["truncated"] is True
    assert entries[2]["summary"] == "dir C:\\tmp"
    assert entries[2]["action"] == {"verb": "ran", "command": "dir C:\\tmp"}
    # A call whose only string arguments are UI labels falls back to them.
    labels_only = st.antigravity_entries([(0, json.dumps(_agy(
        0, "MODEL", "PLANNER_RESPONSE",
        tool_calls=[{"name": "wait", "args": {"Ms": 500, "toolSummary": "Waiting"}}],
    )))])
    assert labels_only[0]["summary"] == "Waiting"


def _agy_conversation(turns: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for i in range(turns):
        rows.append(_agy_prompt(f"prompt {i} " + "p" * i, step=i * 3))
        rows.append(_agy(i * 3 + 1, "MODEL", "PLANNER_RESPONSE", thinking=f"think {i}",
                         tool_calls=[{"name": "list_dir", "args": {"DirectoryPath": f"d{i}"}}]))
        rows.append(_agy(i * 3 + 2, "MODEL", "GENERIC",
                         content=f"Created At: t\nCompleted At: t\nout {i}"))
        rows.append(_agy(i * 3 + 3, "MODEL", "PLANNER_RESPONSE", content=f"answer {i}"))
    return rows


def test_antigravity_pages_concatenate_across_window_boundaries(tmp_path: Path, monkeypatch):
    """Every page, oldest-first, equals one whole parse — nothing split or
    duplicated when the window edge lands mid-turn."""
    monkeypatch.setattr(st, "WINDOW_BYTES", 400)
    path = _write_jsonl(tmp_path / "agy.jsonl", _agy_conversation(20))
    pages = _walk(path, 4, flavor="antigravity")
    assert len(pages) > 1
    assert [e for page in reversed(pages) for e in page] == _parse_whole(
        path, st.antigravity_entries
    )


def test_antigravity_entry_full_text_is_uncapped(tmp_path: Path, monkeypatch):
    """A truncated turn reads back whole — what Chat's copy (#985) and
    read-aloud (#988) call when a page entry came back ``truncated``."""
    monkeypatch.setattr(tf_shared, "ASSISTANT_TEXT_CAP", 40)
    long_reply = "paragraph one. " * 40
    path = _write_jsonl(tmp_path / "agy.jsonl", [
        _agy_prompt("ask"),
        _agy(1, "MODEL", "PLANNER_RESPONSE", thinking="brief", content=long_reply),
    ])
    page = st.transcript_page(path, flavor="antigravity")
    reply = [e for e in page["entries"] if e["kind"] == "assistant"][0]
    assert reply["truncated"] is True and len(reply["text"]) == 40
    full = st.entry_full_text(path, reply["offset"], "antigravity")
    assert full["text"] == long_reply.strip() and full["truncated"] is False
    assert st.entry_full_text(path, 3, "antigravity") is None       # mid-line
    assert st.entry_full_text(path, 99_999, "antigravity") is None  # past EOF
