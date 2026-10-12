"""Chat step labels (#1532): every flavour's ``tool_call`` carries ``label``.

A harness that records the call's own one-line description (Claude's
Bash/PowerShell/Agent ``description``, Copilot's ``powershell`` and Grok's
``run_terminal_command`` ``description``, Antigravity's ``toolSummary`` UI
label) labels the step with it; one that records none (Codex, Pi) falls back
to the call's one-line ``summary``. The fallback is the shared builder's, so
no flavour can ship a call without a label. The strip's live line reads the
description too, while the call is in flight.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from src import session_transcript as st


def _lines(rows: List[Dict[str, Any]]) -> List[Any]:
    return [(i * 100, json.dumps(row)) for i, row in enumerate(rows)]


def _calls(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [e for e in entries if e["kind"] == "tool_call"]


def _claude_call(name: str, inputs: Dict[str, Any], tid: str) -> Dict[str, Any]:
    return {"type": "assistant", "timestamp": "2026-10-12T10:00:01Z",
            "message": {"id": "m-" + tid, "role": "assistant",
                        "content": [{"type": "tool_use", "id": tid, "name": name, "input": inputs}]}}


def test_claude_steps_read_by_their_descriptions():
    entries = st.claude_entries(_lines([
        _claude_call("Bash", {"command": "pytest tests -q", "description": "Run the unit tests"}, "t1"),
        _claude_call("PowerShell", {"command": "Get-ChildItem", "description": "  List   the\nfolder "}, "t2"),
        _claude_call("Agent", {"description": "Find the poll", "prompt": "Search for setInterval"}, "t3"),
        _claude_call("Read", {"file_path": "E:/x/a.py"}, "t4"),
        _claude_call("Bash", {"command": "ls", "description": ""}, "t5"),
    ]))
    calls = _calls(entries)
    assert [c["label"] for c in calls] == [
        "Run the unit tests", "List the folder", "Find the poll", "E:/x/a.py", "ls",
    ]
    # The summary is untouched: it still names the command.
    assert calls[0]["summary"] == "pytest tests -q"


def test_codex_steps_fall_back_to_the_command_summary():
    def codex(payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"type": "response_item", "timestamp": "2026-10-12T10:00:00Z", "payload": payload}

    entries = st.codex_entries(_lines([
        codex({"type": "custom_tool_call", "call_id": "c1", "name": "exec", "input": "Get-Content x"}),
        codex({"type": "function_call", "call_id": "c2", "name": "shell",
               "arguments": "{\"cmd\": \"ls -la\"}"}),
    ]))
    assert [c["label"] for c in _calls(entries)] == ["Get-Content x", "{\"cmd\": \"ls -la\"}"]
    assert all(c["label"] == c["summary"] for c in _calls(entries))


def test_pi_steps_fall_back_to_the_command_summary():
    entries = st.pi_entries(_lines([{
        "type": "message", "id": "m1", "timestamp": "2026-10-12T10:00:00.000Z",
        "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": "p1", "name": "bash", "arguments": {"command": "find docs"}},
        ]},
    }]))
    (call,) = _calls(entries)
    assert call["label"] == call["summary"] == "find docs"


def test_copilot_steps_read_by_their_descriptions():
    def cop(kind: str, data: Dict[str, Any]) -> Dict[str, Any]:
        return {"type": kind, "data": data, "id": "e1", "parentId": None,
                "timestamp": "2026-10-12T10:00:00.000Z"}

    entries = st.copilot_entries(_lines([
        cop("tool.execution_start", {"toolCallId": "c1", "toolName": "powershell",
                                     "arguments": {"command": "git status", "description": "Check the tree"}}),
        cop("tool.execution_start", {"toolCallId": "c2", "toolName": "view",
                                     "arguments": {"path": "E:/x/a.py"}}),
    ]))
    assert [c["label"] for c in _calls(entries)] == ["Check the tree", "E:/x/a.py"]


def test_grok_steps_read_by_their_descriptions():
    def grok(update: Dict[str, Any]) -> Dict[str, Any]:
        return {"timestamp": 1_789_670_620, "method": "session/update",
                "params": {"sessionId": "s", "update": update, "_meta": {}}}

    entries = st.grok_entries(_lines([
        grok({"sessionUpdate": "tool_call", "toolCallId": "g1", "title": "run_terminal_command",
              "rawInput": {"command": "npm test", "description": "Run the JS tests"}}),
        grok({"sessionUpdate": "tool_call", "toolCallId": "g2", "title": "read_file",
              "rawInput": {"target_file": "a.py"}}),
    ]))
    assert [c["label"] for c in _calls(entries)] == ["Run the JS tests", "a.py"]


def test_antigravity_steps_read_by_their_ui_label():
    entries = st.antigravity_entries(_lines([{
        "step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
        "created_at": "2026-10-12T10:00:00Z",
        "tool_calls": [
            {"name": "run_command", "args": {"CommandLine": "\"dir C:\\\\tmp\"", "toolSummary": "\"Listing\""}},
            {"name": "view_file", "args": {"AbsolutePath": "E:/x/a.py"}},
        ],
    }]))
    calls = _calls(entries)
    assert [c["label"] for c in calls] == ["Listing", "E:/x/a.py"]
    assert calls[0]["summary"] == "dir C:\\tmp"


def test_activity_line_names_the_running_step_by_its_description(tmp_path: Path):
    path = tmp_path / "c.jsonl"
    rows = [
        {"type": "user", "timestamp": "2026-10-12T10:00:00Z",
         "message": {"role": "user", "content": "run the tests"}},
        _claude_call("Bash", {"command": "pytest", "description": "Run the unit tests"}, "t1"),
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    got = st.transcript_activity(path, now=path.stat().st_mtime)
    assert got["last"] == "Run the unit tests"
