"""Per-step diffs in the session Chat (#1349).

An Edit / Write / MultiEdit step's ``action`` carries its diff: worked out
from the tool's own input (no line numbers — never invented), or, for
Claude, taken from the ``toolUseResult.structuredPatch`` the harness records
after the call ran (real line numbers). A page trims each step and the whole
diff is fetched on demand through ``/transcript/diff``.

Every path, id and line of content here is synthetic.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src import board
from src.session_transcript import (
    DIFF_INLINE_LINES,
    entry_diff,
    transcript_page,
    transcript_tail,
)
from src.transcript_flavors import _shared as tf_shared
from src.transcript_flavors._shared import diff_counts


def _write_jsonl(path: Path, lines: List[Dict[str, Any]]) -> Path:
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def _prompt(text: str) -> Dict[str, Any]:
    return {"type": "user", "timestamp": "2026-09-30T10:00:00Z",
            "message": {"role": "user", "content": text}}


def _call(name: str, inputs: Dict[str, Any], tid: str, mid: str = "m1") -> Dict[str, Any]:
    return {"type": "assistant", "timestamp": "2026-09-30T10:00:01Z",
            "message": {"id": mid, "role": "assistant",
                        "content": [{"type": "tool_use", "id": tid, "name": name, "input": inputs}]}}


def _result(tid: str, text: str, tool_use_result: Any = None, *, error: bool = False) -> Dict[str, Any]:
    block: Dict[str, Any] = {"type": "tool_result", "tool_use_id": tid, "content": text}
    if error:
        block["is_error"] = True
    row: Dict[str, Any] = {"type": "user", "timestamp": "2026-09-30T10:00:02Z",
                           "message": {"role": "user", "content": [block]}}
    if tool_use_result is not None:
        row["toolUseResult"] = tool_use_result
    return row


def _reply(text: str = "done", mid: str = "m9") -> Dict[str, Any]:
    return {"type": "assistant", "timestamp": "2026-09-30T10:00:03Z",
            "message": {"id": mid, "role": "assistant", "content": [{"type": "text", "text": text}]}}


def _calls(path: Path) -> List[Dict[str, Any]]:
    return [e for e in transcript_page(path)["entries"] if e["kind"] == "tool_call"]


# -------------------------------------------------------- from the tool input


def test_edit_input_diff_has_prefixed_lines_and_no_line_numbers():
    act = tf_shared._tool_action("Edit", {
        "file_path": "src/a.py", "old_string": "keep\nold\nkeep2", "new_string": "keep\nnew\nkeep2",
    })
    diff = act["diff"]
    assert diff["numbered"] is False and diff["truncated"] is False
    assert diff["hunks"] == [{"old_start": None, "new_start": None,
                              "lines": [" keep", "-old", "+new", " keep2"]}]
    assert diff_counts(diff["hunks"]) == (act["added"], act["removed"]) == (1, 1)


def test_multiedit_is_one_combined_diff_not_a_generic_row():
    act = tf_shared._tool_action("MultiEdit", {"file_path": "b.md", "edits": [
        {"old_string": "one", "new_string": "one\ntwo"},
        {"old_string": "x\ny", "new_string": "x"},
    ]})
    assert act["verb"] == "edited" and act["path"] == "b.md"
    assert (act["added"], act["removed"]) == (1, 1)
    assert [h["lines"] for h in act["diff"]["hunks"]] == [[" one", "+two"], [" x", "-y"]]


def test_write_input_is_all_added():
    act = tf_shared._tool_action("Write", {"file_path": "n.txt", "content": "a\nb"})
    assert act["diff"]["hunks"] == [{"old_start": None, "new_start": None, "lines": ["+a", "+b"]}]
    assert act["diff"]["numbered"] is False


def test_each_harnesss_own_edit_and_write_keys_are_read():
    """(#1356) One tool name, several key sets: Pi's ``write`` takes ``path``, Grok's
    ``file_path``; Pi's ``edit`` takes ``oldText``/``newText``, Copilot's
    ``old_str``/``new_str``. Each set was read from a real call or the
    harness's own tool schema."""
    act = tf_shared._tool_action
    grok_edit = act("search_replace", {"file_path": "a", "old_string": "x", "new_string": "y"})
    assert (grok_edit["verb"], grok_edit["path"], grok_edit["added"], grok_edit["removed"]) == ("edited", "a", 1, 1)
    assert act("write", {"file_path": "g", "content": "1\n2"})["added"] == 2         # Grok
    assert act("write", {"path": "p", "content": "1"})["path"] == "p"               # Pi
    cop_edit = act("edit", {"path": "c", "old_str": "beta", "new_str": "gamma"})
    assert (cop_edit["path"], cop_edit["added"], cop_edit["removed"]) == ("c", 1, 1)
    assert act("edit", {"path": "c", "oldText": "a", "newText": "b"})["added"] == 1  # Pi
    assert act("create", {"path": "n", "file_text": "hi\nthere"})["added"] == 2      # Copilot
    assert act("create", {"path": "n"}) is None and act("search_replace", {"file_path": "a"}) is None


def test_an_edit_that_changes_no_line_gets_no_diff():
    act = tf_shared._tool_action("Edit", {"file_path": "a.py", "old_string": "same", "new_string": "same"})
    assert "diff" not in act and (act["added"], act["removed"]) == (0, 0)


def test_full_cap_marks_truncation():
    body = "\n".join("line %d" % i for i in range(50_000))
    act = tf_shared._tool_action("Write", {"file_path": "big.txt", "content": body})
    assert act["diff"]["truncated"] is True
    kept = sum(len(line) + 1 for h in act["diff"]["hunks"] for line in h["lines"])
    assert kept <= tf_shared.DIFF_FULL_BYTES
    assert act["added"] == 50_000   # the count stays whole; only the text is capped


# ------------------------------------------------ Claude's recorded patch


_PATCH = [{"oldStart": 40, "oldLines": 3, "newStart": 40, "newLines": 4,
           "lines": [" a", "-b", "+c", "+d", " e"]}]


def test_claude_edit_takes_the_recorded_patch_with_real_line_numbers(tmp_path):
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _prompt("go"),
        _call("Edit", {"file_path": "E:/repo/src/a.py", "old_string": "b", "new_string": "c\nd"}, "t1"),
        _result("t1", "The file has been updated successfully.",
                {"filePath": "E:\\repo\\src\\a.py", "structuredPatch": _PATCH,
                 "oldString": "b", "newString": "c\nd", "originalFile": None}),
        _reply(),
    ])
    (call,) = _calls(path)
    diff = call["action"]["diff"]
    assert diff["numbered"] is True
    assert diff["hunks"] == [{"old_start": 40, "new_start": 40, "lines": _PATCH[0]["lines"]}]
    assert (call["action"]["added"], call["action"]["removed"]) == (2, 1)
    assert diff["n"] == 0 and isinstance(diff["offset"], int)
    assert call["result"] == "The file has been updated successfully."


def test_claude_write_of_a_new_file_is_all_added_from_line_one(tmp_path):
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _prompt("go"),
        _call("Write", {"file_path": "new.txt", "content": "x\ny"}, "t1"),
        _result("t1", "File created successfully.",
                {"type": "create", "filePath": "new.txt", "content": "x\ny",
                 "structuredPatch": [], "originalFile": None}),
        _reply(),
    ])
    (call,) = _calls(path)
    assert call["action"]["diff"]["numbered"] is True
    assert call["action"]["diff"]["hunks"] == [{"old_start": 0, "new_start": 1, "lines": ["+x", "+y"]}]
    # The Edited card's A badge (#1477) reads this, as the Changed files fold does.
    assert call["action"]["created"] is True


def test_claude_write_over_a_file_counts_its_removed_lines(tmp_path):
    """The input alone says +N −0; the recorded patch knows what went."""
    patch = [{"oldStart": 1, "oldLines": 2, "newStart": 1, "newLines": 1, "lines": ["-old1", "-old2", "+new"]}]
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _prompt("go"),
        _call("Write", {"file_path": "f.txt", "content": "new"}, "t1"),
        _result("t1", "The file has been updated.",
                {"type": "update", "filePath": "f.txt", "content": "new", "structuredPatch": patch}),
        _reply(),
    ])
    (call,) = _calls(path)
    assert (call["action"]["added"], call["action"]["removed"]) == (1, 2)
    assert "created" not in call["action"]


def test_a_failed_edit_keeps_todays_row_with_no_diff(tmp_path):
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _prompt("go"),
        _call("Edit", {"file_path": "a.py", "old_string": "zz", "new_string": "yy"}, "t1"),
        _result("t1", "String to replace not found in file.",
                "Error: String to replace not found in file.", error=True),
        _reply(),
    ])
    (call,) = _calls(path)
    assert call["error"] is True
    assert "diff" not in call["action"]
    assert call["action"]["verb"] == "edited"


def test_a_patch_for_another_file_is_ignored(tmp_path):
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _prompt("go"),
        _call("Edit", {"file_path": "a.py", "old_string": "b", "new_string": "c"}, "t1"),
        _result("t1", "ok", {"filePath": "other.py", "structuredPatch": _PATCH}),
        _reply(),
    ])
    (call,) = _calls(path)
    assert call["action"]["diff"]["numbered"] is False
    assert call["action"]["diff"]["hunks"][0]["lines"] == ["-b", "+c"]


def test_a_malformed_patch_falls_back_to_the_input_diff(tmp_path):
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _prompt("go"),
        _call("Edit", {"file_path": "a.py", "old_string": "b", "new_string": "c"}, "t1"),
        _result("t1", "ok", {"filePath": "a.py", "structuredPatch": [{"oldStart": "1", "lines": []}]}),
        _reply(),
    ])
    (call,) = _calls(path)
    assert call["action"]["diff"]["numbered"] is False


def test_steps_with_no_edit_data_look_as_they_did(tmp_path):
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _prompt("go"),
        _call("Grep", {"pattern": "x"}, "t1"),
        _result("t1", "a.py:1:x"),
        _call("Bash", {"command": "ls"}, "t2", mid="m2"),
        _result("t2", "a.py"),
        _reply(),
    ])
    grep, bash = _calls(path)
    assert "action" not in grep
    assert "diff" not in bash["action"]


# ------------------------------------------------- page caps and full fetch


def _big_edit_transcript(tmp_path: Path, lines: int = 300) -> Path:
    patch = [{"oldStart": 1, "oldLines": 0, "newStart": 1, "newLines": lines,
              "lines": ["+row %d" % i for i in range(lines)]}]
    return _write_jsonl(tmp_path / "t.jsonl", [
        _prompt("go"),
        _call("Edit", {"file_path": "a.py", "old_string": "", "new_string": "x"}, "t1"),
        _result("t1", "ok", {"filePath": "a.py", "structuredPatch": patch}),
        _reply(),
    ])


def test_a_page_trims_each_step_and_the_full_diff_is_one_request_away(tmp_path):
    path = _big_edit_transcript(tmp_path)
    (call,) = _calls(path)
    inline = call["action"]["diff"]
    assert inline["truncated"] is True
    assert sum(len(h["lines"]) for h in inline["hunks"]) == DIFF_INLINE_LINES
    assert "offset" not in call   # a folded entry's own offset stays unexposed
    full = entry_diff(path, inline["offset"], inline["n"], "claude")
    assert full["path"] == "a.py"
    assert full["diff"]["numbered"] is True and full["diff"]["truncated"] is False
    assert sum(len(h["lines"]) for h in full["diff"]["hunks"]) == 300


def test_entry_diff_refuses_a_bad_ref(tmp_path):
    path = _big_edit_transcript(tmp_path)
    (call,) = _calls(path)
    ref = call["action"]["diff"]
    assert entry_diff(path, ref["offset"], 5, "claude") is None
    assert entry_diff(path, ref["offset"] + 1, 0, "claude") is None
    assert entry_diff(path, 10**9, 0, "claude") is None


def test_the_live_tail_trims_too(tmp_path):
    path = _big_edit_transcript(tmp_path)
    out = transcript_tail(path, after=0)
    calls = [e for e in out["entries"] + out["pending"] if e["kind"] == "tool_call"]
    assert calls and calls[0]["action"]["diff"]["truncated"] is True
    assert calls[0]["action"]["diff"]["n"] == 0


# ------------------------------------------------------------------ route


@pytest.fixture
def _bypass_gate(monkeypatch):
    from app.webapp import middleware
    monkeypatch.setattr(
        middleware, "LOOPBACK_HOSTS",
        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
    )


def _serve(overrides, monkeypatch, path: Path) -> None:
    overrides["session"].list_sessions.return_value = [{
        "session_id": "s1", "kind": "pty", "agent": "claude", "alive": True,
        "project_dir": "E:/work/proj", "started_at": 1_789_000_000,
    }]
    monkeypatch.setattr(board, "state_row_for_session",
                        lambda live, rows, sid: {"transcript_path": str(path)})


def test_transcript_diff_path_classified_passkey():
    """Its own guard row: '.../transcript/diff' ends with neither
    '/transcript' nor '/transcript/entry', so no sibling rule covers it."""
    from app.webapp.middleware import _terminal_guard_level
    assert _terminal_guard_level("/api/claude-code/sessions/abc/transcript/diff") == "passkey"


def test_transcript_diff_refused_off_tailnet(webapp_client):
    client, _, _ = webapp_client
    assert client.get("/api/claude-code/sessions/s1/transcript/diff?offset=0").status_code == 403


class TestTranscriptDiffEndpoint:

    def test_returns_the_whole_diff_and_never_logs_content(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path, caplog
    ):
        client, _, overrides = webapp_client
        path = _big_edit_transcript(tmp_path)
        _serve(overrides, monkeypatch, path)
        page = client.get("/api/claude-code/sessions/s1/transcript").json()
        (call,) = [e for e in page["entries"] if e["kind"] == "tool_call"]
        ref = call["action"]["diff"]
        with caplog.at_level(logging.INFO):
            body = client.get(
                f"/api/claude-code/sessions/s1/transcript/diff?offset={ref['offset']}&n={ref['n']}"
            ).json()
        assert body["available"] is True and body["path"] == "a.py"
        assert sum(len(h["lines"]) for h in body["diff"]["hunks"]) == 300
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "row 1" not in joined and "transcript diff s1" in joined

    def test_a_bad_ref_is_diff_not_found(self, webapp_client, _bypass_gate, monkeypatch, tmp_path):
        client, _, overrides = webapp_client
        _serve(overrides, monkeypatch, _big_edit_transcript(tmp_path))
        body = client.get("/api/claude-code/sessions/s1/transcript/diff?offset=0&n=0").json()
        assert body["available"] is False and body["reason"] == "diff_not_found"

    def test_offset_is_required(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = []
        assert client.get("/api/claude-code/sessions/s1/transcript/diff").status_code == 422

    def test_unknown_session_shares_the_page_routes_reasons(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = []
        body = client.get("/api/claude-code/sessions/s1/transcript/diff?offset=0").json()
        assert body["available"] is False and body["reason"] == "session_not_found"
