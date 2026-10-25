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

from src import board
from src.session_view import changes as session_changes
from src.session_view.changes import changed_files, file_steps
from src.session_view.transcript import transcript_page


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


# ------------------------------------------- the file itself (#1689)

_CONTENT = "/api/claude-code/sessions/s1/changed-files/content"
_OPEN = "/api/claude-code/sessions/s1/changed-files/open"


def test_file_routes_classified_passkey():
    """Each a row of its own: neither ends with '/changed-files' or '/diff'."""
    from app.webapp.middleware import _terminal_guard_level
    assert _terminal_guard_level("/api/claude-code/sessions/abc/changed-files/content") == "passkey"
    assert _terminal_guard_level("/api/claude-code/sessions/abc/changed-files/open") == "passkey"


def test_file_routes_refused_off_tailnet(webapp_client):
    client, _, _ = webapp_client
    assert client.get(_CONTENT + "?path=x").status_code == 403
    assert client.post(_OPEN, json={"path": "x"}).status_code == 403


def test_a_relative_recorded_path_resolves_against_the_project(tmp_path, monkeypatch):
    """A Codex patch names files from its working folder; the fold resolves
    them, and so does the lookup, so a Chat step's relative path and the
    list's absolute key both find the file."""
    project = tmp_path / "proj"
    edit = session_changes.Edit("src/x.py", [{"lines": ["+a"]}], True, None, added=1)
    absolute = str(project).replace("\\", "/") + "/src/x.py"
    for requested in ("src/x.py", absolute, absolute.upper()):
        monkeypatch.setattr(session_changes, "_flavor_edits", lambda path, flavor: (iter([edit]), False))
        assert session_changes.changed_file_path(tmp_path / "t.jsonl", requested, "codex", str(project)) == absolute
    # With no project folder the recorded path stays relative: no file on disk.
    monkeypatch.setattr(session_changes, "_flavor_edits", lambda path, flavor: (iter([edit]), False))
    assert session_changes.changed_file_path(tmp_path / "t.jsonl", "src/x.py", "codex", None) is None


class TestChangedFileContent:

    def _setup(self, webapp_client, monkeypatch, tmp_path):
        client, _, overrides = webapp_client
        project = tmp_path / "proj"
        _serve(overrides, monkeypatch, _session(tmp_path, project), project)
        return client, project

    def test_a_changed_file_is_served_whole(self, webapp_client, _bypass_gate, monkeypatch, tmp_path, caplog):
        client, project = self._setup(webapp_client, monkeypatch, tmp_path)
        (project / "b.md").write_bytes("\ufeff# Title\n\nsecret-body-text\n".encode("utf-8"))
        with caplog.at_level(logging.INFO):
            body = client.get(_CONTENT, params={"path": str(project / "b.md")}).json()
        assert body["available"] is True and body["reason"] is None
        assert body["text"] == "# Title\n\nsecret-body-text\n"   # the BOM dropped
        assert body["path"] == str(project / "b.md").replace("/", "\\")
        mine = [r for r in caplog.records if "changed file" in r.getMessage()]
        joined = "\n".join(r.getMessage() for r in mine)
        assert "changed file content s1" in joined
        assert "secret-body-text" not in joined and "b.md" not in joined
        assert mine and all(r.levelno == logging.INFO for r in mine)

    def test_the_path_may_differ_in_case_and_slashes(self, webapp_client, _bypass_gate, monkeypatch, tmp_path):
        client, project = self._setup(webapp_client, monkeypatch, tmp_path)
        key = str(project / "src" / "a.py").replace("\\", "/").upper()
        body = client.get(_CONTENT, params={"path": key}).json()
        assert body["available"] is True and body["text"].splitlines() == ["x"]

    @pytest.mark.parametrize("requested", [
        "C:/Windows/win.ini",                 # anywhere else
        "{project}/notes.txt",                # on disk, never edited
        "{project}/src/../b.md",              # traversal onto an edited file
        "b.md",                               # relative, for an agent that records absolute paths
        "{transcript}",                       # the transcript itself
    ])
    def test_a_path_outside_the_edited_set_is_file_not_found(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path, requested,
    ):
        client, project = self._setup(webapp_client, monkeypatch, tmp_path)
        (project / "notes.txt").write_text("not edited\n", encoding="utf-8")
        path = requested.format(project=str(project).replace("\\", "/"), transcript=str(tmp_path / "t.jsonl"))
        body = client.get(_CONTENT, params={"path": path}).json()
        assert body["available"] is False and body["reason"] == "file_not_found"
        assert "text" not in body

    def test_a_file_gone_from_disk_is_deleted(self, webapp_client, _bypass_gate, monkeypatch, tmp_path):
        client, project = self._setup(webapp_client, monkeypatch, tmp_path)
        body = client.get(_CONTENT, params={"path": str(project / "old.txt")}).json()
        assert body["available"] is False and body["reason"] == "deleted"

    def test_an_oversize_file_is_too_large_with_its_size(self, webapp_client, _bypass_gate, monkeypatch, tmp_path):
        client, project = self._setup(webapp_client, monkeypatch, tmp_path)
        big = "y" * (session_changes.VIEW_MAX_BYTES + 1)
        (project / "b.md").write_text(big, encoding="utf-8")
        body = client.get(_CONTENT, params={"path": str(project / "b.md")}).json()
        assert body["available"] is False and body["reason"] == "too_large"
        assert body["size"] == len(big) and "text" not in body
        (project / "b.md").write_text(big[:-1], encoding="utf-8")   # exactly at the cap is served
        assert client.get(_CONTENT, params={"path": str(project / "b.md")}).json()["available"] is True

    @pytest.mark.parametrize("data", [b"PK\x03\x04\x00\x00binary", b"caf\xe9 latin-1\n"])
    def test_a_binary_or_non_utf8_file_is_not_text(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path, data,
    ):
        client, project = self._setup(webapp_client, monkeypatch, tmp_path)
        (project / "b.md").write_bytes(data)
        body = client.get(_CONTENT, params={"path": str(project / "b.md")}).json()
        assert body["available"] is False and body["reason"] == "not_text"

    def test_an_unreadable_file_is_read_failed(self, webapp_client, _bypass_gate, monkeypatch, tmp_path, caplog):
        client, project = self._setup(webapp_client, monkeypatch, tmp_path)
        from app.webapp.routers import session_transcript as routes

        def _denied(target, *a, **kw):
            raise PermissionError("denied")
        monkeypatch.setattr(routes, "read_text_file", _denied)
        with caplog.at_level(logging.INFO):
            body = client.get(_CONTENT, params={"path": str(project / "b.md")}).json()
        assert body["available"] is False and body["reason"] == "read_failed"
        assert any(r.levelno == logging.WARNING and "read failed" in r.getMessage() for r in caplog.records)

    def test_path_is_required(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = []
        assert client.get(_CONTENT).status_code == 422


class TestChangedFileOpen:

    @pytest.fixture
    def launches(self, monkeypatch):
        from app.webapp.routers import session_transcript as routes
        from src.webapp_config import SESSION_HOST_PORT_ENV
        calls: List[tuple] = []
        monkeypatch.setattr(routes, "is_vscode_installed", lambda: True)
        monkeypatch.setattr(routes, "open_file", lambda p: calls.append(("vscode", str(p))) or 1)
        monkeypatch.setattr(routes, "select_in_explorer", lambda p: calls.append(("explorer", str(p))) or 2)
        monkeypatch.delenv(SESSION_HOST_PORT_ENV, raising=False)
        return calls

    def _setup(self, webapp_client, monkeypatch, tmp_path):
        client, _, overrides = webapp_client
        project = tmp_path / "proj"
        _serve(overrides, monkeypatch, _session(tmp_path, project), project)
        return client, project

    def test_opens_a_changed_file_in_vscode(self, webapp_client, _bypass_gate, monkeypatch, tmp_path, launches):
        client, project = self._setup(webapp_client, monkeypatch, tmp_path)
        body = client.post(_OPEN, json={"path": str(project / "b.md")}).json()
        assert body["opened"] is True and body["with"] == "vscode"
        assert launches == [("vscode", str(project / "b.md"))]

    def test_without_vscode_explorer_shows_it(self, webapp_client, _bypass_gate, monkeypatch, tmp_path, launches):
        from app.webapp.routers import session_transcript as routes
        monkeypatch.setattr(routes, "is_vscode_installed", lambda: False)
        client, project = self._setup(webapp_client, monkeypatch, tmp_path)
        body = client.post(_OPEN, json={"path": str(project / "b.md")}).json()
        assert body["opened"] is True and body["with"] == "explorer"
        assert launches == [("explorer", str(project / "b.md"))]

    def test_only_the_pc_itself_may_ask(self, webapp_client, _bypass_gate, monkeypatch, tmp_path, launches):
        from app.webapp.routers import session_transcript as routes
        monkeypatch.setattr(routes, "is_pc_itself", lambda host, headers: False)
        client, project = self._setup(webapp_client, monkeypatch, tmp_path)
        assert client.post(_OPEN, json={"path": str(project / "b.md")}).status_code == 403
        assert launches == []

    def test_a_disposable_instance_launches_nothing(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path, launches,
    ):
        from src.webapp_config import SESSION_HOST_PORT_ENV
        monkeypatch.setenv(SESSION_HOST_PORT_ENV, "18446")
        client, project = self._setup(webapp_client, monkeypatch, tmp_path)
        body = client.post(_OPEN, json={"path": str(project / "b.md")}).json()
        assert body["opened"] is False and body["with"] == "none"
        assert launches == []

    @pytest.mark.parametrize("requested, reason", [
        ("C:/Windows/System32/cmd.exe", "file_not_found"),
        ("{project}/old.txt", "deleted"),
    ])
    def test_a_refused_file_launches_nothing(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path, launches, requested, reason,
    ):
        client, project = self._setup(webapp_client, monkeypatch, tmp_path)
        path = requested.format(project=str(project).replace("\\", "/"))
        body = client.post(_OPEN, json={"path": path}).json()
        assert body["opened"] is False and body["reason"] == reason
        assert launches == []

    def test_path_is_required(self, webapp_client, _bypass_gate, launches):
        client, _, _ = webapp_client
        assert client.post(_OPEN, json={}).status_code == 400
