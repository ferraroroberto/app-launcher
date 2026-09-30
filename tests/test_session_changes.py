"""A session's Changed files (#1349): every file its edits touched, folded
from the transcript — never git. The Claude fold and its routes; the other
agents' fold is ``test_session_changes_flavors.py`` (#1356).

Every path, id and line of content here is synthetic.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src import board, session_changes
from src.session_changes import changed_files, file_steps
from src.session_transcript import transcript_page


def _write_jsonl(path: Path, lines: List[Dict[str, Any]]) -> Path:
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def _call(name: str, inputs: Dict[str, Any], tid: str) -> Dict[str, Any]:
    return {"type": "assistant", "timestamp": "2026-09-30T10:00:01Z",
            "message": {"id": "m-" + tid, "role": "assistant",
                        "content": [{"type": "tool_use", "id": tid, "name": name, "input": inputs}]}}


def _result(tid: str, tur: Any, *, error: bool = False, sidechain: bool = False,
            ts: str = "2026-09-30T10:00:02Z") -> Dict[str, Any]:
    block: Dict[str, Any] = {"type": "tool_result", "tool_use_id": tid, "content": "ok"}
    if error:
        block["is_error"] = True
    return {"type": "user", "timestamp": ts, "isSidechain": sidechain,
            "message": {"role": "user", "content": [block]}, "toolUseResult": tur}


def _patch(old_start: int, lines: List[str]) -> List[Dict[str, Any]]:
    return [{"oldStart": old_start, "oldLines": 0, "newStart": old_start, "newLines": 0, "lines": lines}]


def _session(tmp_path: Path, project: Path) -> Path:
    """Three files: one created then edited, one modified (by a sub-agent
    too), one modified and since deleted; plus a failed edit and a read."""
    a, b, gone = project / "src" / "a.py", project / "b.md", project / "old.txt"
    a.parent.mkdir(parents=True)
    a.write_text("x\n", encoding="utf-8")
    b.write_text("y\n", encoding="utf-8")
    return _write_jsonl(tmp_path / "t.jsonl", [
        {"type": "user", "timestamp": "2026-09-30T10:00:00Z", "message": {"role": "user", "content": "go"}},
        _call("Write", {"file_path": str(a), "content": "l1\nl2"}, "t1"),
        _result("t1", {"type": "create", "filePath": str(a), "content": "l1\nl2", "structuredPatch": []}),
        _call("Edit", {"file_path": str(a), "old_string": "l2", "new_string": "l2\nl3"}, "t2"),
        _result("t2", {"filePath": str(a), "structuredPatch": _patch(2, [" l2", "+l3"])},
                ts="2026-09-30T10:05:00Z"),
        _call("Edit", {"file_path": str(b), "old_string": "q", "new_string": "r"}, "t3"),
        _result("t3", {"filePath": str(b), "structuredPatch": _patch(7, ["-q", "+r"])}),
        _call("Edit", {"file_path": str(b), "old_string": "s", "new_string": "t"}, "t4"),
        _result("t4", {"filePath": str(b).replace("\\", "/").upper(),
                       "structuredPatch": _patch(9, ["-s", "+t", "+u"])}, sidechain=True),
        _call("Edit", {"file_path": str(gone), "old_string": "z", "new_string": ""}, "t5"),
        _result("t5", {"filePath": str(gone), "structuredPatch": _patch(1, ["-z"])}),
        _call("Edit", {"file_path": str(b), "old_string": "nope", "new_string": "x"}, "t6"),
        _result("t6", "Error: String to replace not found in file.", error=True),
        _call("Read", {"file_path": str(b)}, "t7"),
        _result("t7", {"type": "text", "file": {"filePath": str(b)}}),
    ])


def test_fold_lists_every_touched_file_with_badges_and_counts(tmp_path):
    project = tmp_path / "proj"
    path = _session(tmp_path, project)
    out = changed_files(path, str(project))
    rows = {r["path"]: r for r in out["files"]}
    assert list(rows) == ["b.md", "old.txt", "src/a.py"]   # relative, sorted
    assert (rows["src/a.py"]["status"], rows["src/a.py"]["additions"], rows["src/a.py"]["deletions"],
            rows["src/a.py"]["steps"]) == ("A", 3, 0, 2)
    # Two edits, one by a sub-agent, one recorded with the other slash and
    # case: still one Windows file.
    assert (rows["b.md"]["status"], rows["b.md"]["additions"], rows["b.md"]["deletions"],
            rows["b.md"]["steps"]) == ("M", 3, 2, 2)
    assert rows["old.txt"]["status"] == "D"
    assert out["counts"] == {"additions": 6, "deletions": 3}
    assert out["partial"] is False and out["project_exists"] is True


def test_totals_match_the_per_step_counts(tmp_path):
    """The acceptance criterion: the panel's totals are the sum of what the
    Chat steps say, failed steps excluded on both sides."""
    project = tmp_path / "proj"
    path = _session(tmp_path, project)
    steps = [e["action"] for e in transcript_page(path)["entries"]
             if e["kind"] == "tool_call" and e.get("action", {}).get("verb") in ("edited", "wrote")
             and not e.get("error")]
    out = changed_files(path, str(project))
    assert out["counts"] == {
        "additions": sum(a["added"] for a in steps),
        "deletions": sum(a["removed"] for a in steps),
    }


def test_a_gone_project_folder_marks_nothing_deleted(tmp_path):
    """Once a worktree is removed every file is missing: that says nothing
    about what the session did, so no D badge — the panel says why."""
    project = tmp_path / "proj"
    path = _session(tmp_path, project)
    out = changed_files(path, str(tmp_path / "removed-worktree"))
    assert {r["status"] for r in out["files"]} == {"A", "M"}
    assert out["project_exists"] is False
    # Outside the project the full path is shown.
    assert all(Path(r["path"]).is_absolute() for r in out["files"])


def test_no_edits_is_an_empty_fold(tmp_path):
    path = _write_jsonl(tmp_path / "t.jsonl", [
        {"type": "user", "timestamp": "2026-09-30T10:00:00Z", "message": {"role": "user", "content": "hi"}},
    ])
    out = changed_files(path, None)
    assert out["files"] == [] and out["counts"] == {"additions": 0, "deletions": 0}


def test_file_steps_are_that_files_edits_in_order(tmp_path):
    project = tmp_path / "proj"
    path = _session(tmp_path, project)
    key = next(r["key"] for r in changed_files(path, str(project))["files"] if r["path"] == "src/a.py")
    out = file_steps(path, key)
    assert [s["created"] for s in out["steps"]] == [True, False]
    assert out["steps"][0]["diff"]["hunks"] == [{"old_start": 0, "new_start": 1, "lines": ["+l1", "+l2"]}]
    assert out["steps"][1]["diff"]["hunks"][0]["lines"] == [" l2", "+l3"]
    assert out["steps"][1]["timestamp"] == "2026-09-30T10:05:00Z"
    assert out["truncated"] is False


def test_file_steps_refuses_a_file_the_session_never_edited(tmp_path):
    project = tmp_path / "proj"
    path = _session(tmp_path, project)
    assert file_steps(path, str(project / "elsewhere.py")) is None
    # A file it only read is not a changed file either.
    assert file_steps(path, str(tmp_path / "unrelated")) is None


def test_file_steps_cap_marks_truncation(tmp_path):
    project = tmp_path / "proj"
    path = _session(tmp_path, project)
    key = next(r["key"] for r in changed_files(path, str(project))["files"] if r["path"] == "src/a.py")
    out = file_steps(path, key, max_bytes=8)
    assert out["truncated"] is True and len(out["steps"]) == 1


def test_a_transcript_past_the_ceiling_reads_its_newest_part(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    path = _session(tmp_path, project)
    monkeypatch.setattr(session_changes, "SCAN_CEILING", path.stat().st_size // 2)
    out = changed_files(path, str(project))
    assert out["partial"] is True
    assert "src/a.py" not in {r["path"] for r in out["files"]}   # its edits sit in the older half


# ------------------------------------------------------------------ routes


@pytest.fixture
def _bypass_gate(monkeypatch):
    from app.webapp import middleware
    monkeypatch.setattr(
        middleware, "LOOPBACK_HOSTS",
        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
    )


def _serve(overrides, monkeypatch, path: Path, project: Path, agent: str = "claude") -> None:
    overrides["session"].list_sessions.return_value = [{
        "session_id": "s1", "kind": "pty", "agent": agent, "alive": True,
        "project_dir": str(project), "started_at": 1_789_000_000,
    }]
    monkeypatch.setattr(board, "state_row_for_session",
                        lambda live, rows, sid: {"transcript_path": str(path)})


def test_changed_files_paths_classified_passkey():
    """Two rows: '/changed-files/diff' does not end with '/changed-files'."""
    from app.webapp.middleware import _terminal_guard_level
    assert _terminal_guard_level("/api/claude-code/sessions/abc/changed-files") == "passkey"
    assert _terminal_guard_level("/api/claude-code/sessions/abc/changed-files/diff") == "passkey"


def test_changed_files_refused_off_tailnet(webapp_client):
    client, _, _ = webapp_client
    assert client.get("/api/claude-code/sessions/s1/changed-files").status_code == 403
    assert client.get("/api/claude-code/sessions/s1/changed-files/diff?path=x").status_code == 403


class TestChangedFilesEndpoints:

    def test_list_then_one_files_diff(self, webapp_client, _bypass_gate, monkeypatch, tmp_path, caplog):
        client, _, overrides = webapp_client
        project = tmp_path / "proj"
        _serve(overrides, monkeypatch, _session(tmp_path, project), project)
        with caplog.at_level(logging.INFO):
            body = client.get("/api/claude-code/sessions/s1/changed-files").json()
        assert body["available"] is True and body["source"] == "transcript"
        assert [r["path"] for r in body["files"]] == ["b.md", "old.txt", "src/a.py"]
        key = body["files"][2]["key"]
        diff = client.get("/api/claude-code/sessions/s1/changed-files/diff", params={"path": key}).json()
        assert diff["available"] is True and len(diff["steps"]) == 2
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "changed files s1" in joined and "a.py" not in joined

    def test_unknown_file_is_file_not_found(self, webapp_client, _bypass_gate, monkeypatch, tmp_path):
        client, _, overrides = webapp_client
        project = tmp_path / "proj"
        _serve(overrides, monkeypatch, _session(tmp_path, project), project)
        body = client.get("/api/claude-code/sessions/s1/changed-files/diff",
                          params={"path": "C:/Windows/win.ini"}).json()
        assert body["available"] is False and body["reason"] == "file_not_found"

    def test_an_agent_without_a_transcript_reader_is_unsupported(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path
    ):
        client, _, overrides = webapp_client
        project = tmp_path / "proj"
        _serve(overrides, monkeypatch, _session(tmp_path, project), project, agent="aider")
        body = client.get("/api/claude-code/sessions/s1/changed-files").json()
        assert body["available"] is False and body["reason"] == "unsupported_agent"

    def test_no_transcript_is_said(self, webapp_client, _bypass_gate, monkeypatch, tmp_path):
        client, _, overrides = webapp_client
        _serve(overrides, monkeypatch, tmp_path / "missing.jsonl", tmp_path)
        body = client.get("/api/claude-code/sessions/s1/changed-files").json()
        assert body["available"] is False and body["reason"] == "no_transcript"

    def test_path_is_required(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = []
        assert client.get("/api/claude-code/sessions/s1/changed-files/diff").status_code == 422
