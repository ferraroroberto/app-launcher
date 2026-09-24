"""Chat's ExitPlanMode plan card (#1151): the plan and how it was answered.

The four result shapes pinned here are the ones characterized across 153
answered calls on the dev box and re-probed on Claude Code 2.1.281 (evidence
on the issue). Every transcript here is synthetic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src import session_transcript as st
from src.plan_review import PLAN_CAP, plan_from_input, plan_outcome

_REJECT = ("The user doesn't want to proceed with this tool use. The tool use was rejected "
           "(eg. if it was a file edit, the new_string was NOT written to the file).")
_SAID = _REJECT + (" To tell you how to proceed, the user said:\nUse the word hello instead\n\n"
                   "Note: The user's next message may contain a correction or preference.")
_NOT_PLAN = ("<tool_use_error>You are not in plan mode. To enter plan mode, call the "
             "EnterPlanMode tool first.</tool_use_error>")
_APPROVED_TUR = {"plan": "# Plan", "filePath": "C:/plans/x.md", "isAgent": False}


def test_plan_comes_from_the_input_and_is_capped():
    assert plan_from_input({"plan": "  # Plan\n- a  ", "planFilePath": "x"}) == ("# Plan\n- a", False)
    text, truncated = plan_from_input({"plan": "x" * (PLAN_CAP + 5)})
    assert len(text) == PLAN_CAP and truncated is True
    for bad in (None, {}, {"plan": ""}, {"plan": 3}, {"allowedPrompts": []}):
        assert plan_from_input(bad) is None, bad


@pytest.mark.parametrize("call,tur,text,err,want", [
    ("ExitPlanMode", _APPROVED_TUR, "User has approved your plan.", False, {"plan_outcome": "approved"}),
    ("ExitPlanMode", dict(_APPROVED_TUR, planWasEdited=True), "ok", False,
     {"plan_outcome": "approved", "plan_edited": True}),
    # An approval is recognisable without its call (the call on an earlier page).
    (None, _APPROVED_TUR, "User has approved your plan.", False, {"plan_outcome": "approved"}),
    ("ExitPlanMode", "Error: ...", _SAID, True,
     {"plan_outcome": "sent_back", "plan_feedback": "Use the word hello instead"}),
    ("ExitPlanMode", "Error: ...", _REJECT, True, {"plan_outcome": "declined"}),
    ("ExitPlanMode", "Error: ...", _NOT_PLAN, True, {"plan_outcome": "not_shown"}),
    # Generic rejection text is never read as a plan answer for another tool.
    ("Edit", "Error: ...", _SAID, True, None),
    (None, "Error: ...", _SAID, True, None),
    ("Read", {"file": {}}, "body", False, None),
])
def test_plan_outcome_shapes(call, tur, text, err, want):
    assert plan_outcome(call, tur, text, err) == want


def _jsonl(path: Path, rows: List[Dict[str, Any]]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def _lines(path: Path):
    raw, pos, out = path.read_bytes(), 0, []
    for chunk in raw.split(b"\n"):
        if chunk.strip():
            out.append((pos, chunk.decode("utf-8")))
        pos += len(chunk) + 1
    return out


def _call(tid: str, plan: str, mid: str) -> Dict[str, Any]:
    return {"type": "assistant", "timestamp": "2026-09-24T03:00:00Z",
            "message": {"id": mid, "role": "assistant", "content": [
                {"type": "tool_use", "id": tid, "name": "ExitPlanMode",
                 "input": {"plan": plan, "planFilePath": "C:/plans/x.md"}}]}}


def _result(tid: str, text: str, tur: Any, error: bool = False) -> Dict[str, Any]:
    block = {"type": "tool_result", "tool_use_id": tid, "content": text}
    if error:
        block["is_error"] = True
    return {"type": "user", "timestamp": "2026-09-24T03:00:09Z", "toolUseResult": tur,
            "message": {"role": "user", "content": [block]}}


def test_claude_entries_carry_the_plan_and_its_outcome(tmp_path: Path):
    path = _jsonl(tmp_path / "t.jsonl", [
        _call("p1", "# Build it\n- step one", "m1"),
        _result("p1", _SAID, "Error: " + _SAID, error=True),
        _call("p2", "# Build it again", "m2"),
        _result("p2", "User has approved your plan.", _APPROVED_TUR),
    ])
    first, second = st.claude_entries(_lines(path))
    assert first["plan"] == "# Build it\n- step one" and first["plan_truncated"] is False
    assert first["call_id"] == "p1"
    assert first["plan_outcome"] == "sent_back" and first["plan_feedback"] == "Use the word hello instead"
    assert first["error"] is True            # the harness's own flag is kept as-is
    assert second["plan_outcome"] == "approved" and "error" not in second


def test_a_pending_plan_has_no_outcome_and_a_standalone_approval_carries_one(tmp_path: Path):
    path = _jsonl(tmp_path / "t.jsonl", [_call("p1", "# Plan", "m1")])
    (pending,) = st.claude_entries(_lines(path))
    assert pending["result"] is None and "plan_outcome" not in pending
    path = _jsonl(tmp_path / "u.jsonl", [_result("p1", "User has approved your plan.", _APPROVED_TUR)])
    (orphan,) = st.claude_entries(_lines(path))
    assert orphan["kind"] == "tool_result" and orphan["plan_outcome"] == "approved"
