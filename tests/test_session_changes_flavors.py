"""Changed files for the non-Claude agents (#1356), folded from each
flavour's own entry builder.

Every path, id and line of content here is synthetic. One synthetic
transcript per flavour; each pins the same three facts — the fold lists what
the successful edit/write steps touched, its totals equal the sum of the
steps a Chat page shows (:func:`src.session_transcript.transcript_page`), and
a step its harness marked failed counts nowhere.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src import board, session_changes
from src.session_changes import changed_files, file_steps
from src.session_transcript import transcript_page
from src.transcript_flavors.codex import _js_unescape, _patch_literals, parse_apply_patch


def _write_jsonl(path: Path, lines: List[Dict[str, Any]]) -> Path:
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def _chat_edit_sum(path: Path, flavor: str) -> Dict[str, int]:
    """What the Chat steps of this transcript add up to."""
    page = transcript_page(path, flavor=flavor, limit=100)
    steps = [
        e["action"] for e in page["entries"]
        if e.get("kind") == "tool_call" and not e.get("error")
        and (e.get("action") or {}).get("verb") in ("edited", "wrote") and (e["action"].get("diff") or {}).get("hunks")
    ]
    return {"additions": sum(a["added"] for a in steps), "deletions": sum(a["removed"] for a in steps)}


def _rows(found: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {r["path"]: r for r in found["files"]}


# ----------------------------------------------------------------------- Pi


def _pi_call(tid: str, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "message", "id": "a" + tid, "timestamp": "2026-09-30T10:00:01.000Z", "message": {
        "role": "assistant", "content": [{"type": "toolCall", "id": tid, "name": name, "arguments": args}]}}


def _pi_result(tid: str, *, error: bool = False) -> Dict[str, Any]:
    return {"type": "message", "id": "r" + tid, "timestamp": "2026-09-30T10:00:02.000Z", "message": {
        "role": "toolResult", "toolCallId": tid, "isError": error, "content": [{"type": "text", "text": "ok"}]}}


def test_pi_fold_lists_edits_and_writes_and_skips_a_failed_step(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    a, b = project / "a.py", project / "b.md"
    a.write_text("x\n", encoding="utf-8")
    b.write_text("y\n", encoding="utf-8")
    path = _write_jsonl(tmp_path / "pi.jsonl", [
        _pi_call("t1", "edit", {"path": str(a), "oldText": "one", "newText": "one\ntwo"}), _pi_result("t1"),
        _pi_call("t2", "edit", {"path": str(a), "edits": [{"oldText": "p", "newText": "q"}]}), _pi_result("t2"),
        _pi_call("t3", "write", {"path": str(b), "content": "l1\nl2\nl3"}), _pi_result("t3"),
        _pi_call("t4", "edit", {"path": str(b), "oldText": "l1", "newText": "boom"}), _pi_result("t4", error=True),
        _pi_call("t5", "read", {"path": str(b)}), _pi_result("t5"),
    ])
    found = changed_files(path, str(project), "pi")
    rows = _rows(found)
    assert sorted(rows) == ["a.py", "b.md"]
    assert (rows["a.py"]["steps"], rows["a.py"]["additions"], rows["a.py"]["deletions"]) == (2, 2, 1)
    assert (rows["b.md"]["steps"], rows["b.md"]["additions"], rows["b.md"]["deletions"]) == (1, 3, 0)
    assert found["counts"] == _chat_edit_sum(path, "pi")
    steps = file_steps(path, rows["a.py"]["key"], flavor="pi", project_dir=str(project))
    assert [s["diff"]["numbered"] for s in steps["steps"]] == [False, False]
    assert file_steps(path, str(project / "never.py"), flavor="pi", project_dir=str(project)) is None


# --------------------------------------------------------------------- Grok


def _grok(update: Dict[str, Any]) -> Dict[str, Any]:
    return {"timestamp": 1_789_670_620, "method": "session/update",
            "params": {"sessionId": "s", "update": update, "_meta": {}}}


def _grok_call(tid: str, title: str, raw: Dict[str, Any], status: str = "completed") -> List[Dict[str, Any]]:
    return [_grok({"sessionUpdate": "tool_call", "toolCallId": tid, "title": title, "rawInput": raw}),
            _grok({"sessionUpdate": "tool_call_update", "toolCallId": tid, "status": status, "content": []})]


def test_grok_fold_reads_search_replace_and_write_by_file_path(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    f, g = project / "f.txt", project / "g.txt"
    rows = (
        _grok_call("t1", "search_replace", {"file_path": str(f), "old_string": "a", "new_string": "a\nb"})
        + _grok_call("t2", "write", {"file_path": str(g), "content": "1\n2"})
        + _grok_call("t3", "search_replace", {"file_path": str(g), "old_string": "1", "new_string": "9"}, status="failed")
        + _grok_call("t4", "read_file", {"target_file": str(g)})
    )
    path = _write_jsonl(tmp_path / "updates.jsonl", rows)
    found = changed_files(path, str(project), "grok")
    by = _rows(found)
    assert sorted(by) == ["f.txt", "g.txt"]
    assert (by["f.txt"]["additions"], by["f.txt"]["deletions"]) == (1, 0)
    assert by["g.txt"]["steps"] == 1 and by["g.txt"]["additions"] == 2     # the failed edit is not a step
    assert found["counts"] == _chat_edit_sum(path, "grok")


# ------------------------------------------------------------- Antigravity


def _agy_call(step: int, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    return {"step_index": step, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
            "created_at": "2026-09-30T10:00:01Z", "tool_calls": [{"name": name, "args": args}]}


def _agy_result(step: int, status: str = "DONE") -> Dict[str, Any]:
    return {"step_index": step, "source": "MODEL", "type": "GENERIC", "status": status,
            "created_at": "2026-09-30T10:00:02Z", "content": "done"}


def test_antigravity_fold_pairs_results_positionally_and_skips_an_error(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    ok, bad = project / "ok.txt", project / "bad.txt"
    path = _write_jsonl(tmp_path / "transcript_full.jsonl", [
        _agy_call(1, "write_to_file", {"TargetFile": str(ok), "CodeContent": "a\nb\nc"}), _agy_result(2),
        _agy_call(3, "write_to_file", {"TargetFile": str(bad), "CodeContent": "z"}), _agy_result(4, "ERROR"),
    ])
    found = changed_files(path, str(project), "antigravity")
    assert [r["path"] for r in found["files"]] == ["ok.txt"]
    assert found["counts"] == {"additions": 3, "deletions": 0} == _chat_edit_sum(path, "antigravity")


# ------------------------------------------------------------------ Copilot


def _cop(kind: str, data: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": kind, "data": data, "id": "e", "parentId": None, "timestamp": "2026-09-30T10:00:01.000Z"}


def _cop_call(tid: str, name: str, args: Dict[str, Any], success: bool = True) -> List[Dict[str, Any]]:
    return [_cop("tool.execution_start", {"toolCallId": tid, "toolName": name, "arguments": args}),
            _cop("tool.execution_complete", {"toolCallId": tid, "success": success, "result": {"content": "ok"}})]


def test_copilot_fold_reads_edit_and_create_keys_probed_from_the_cli(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    a, n = project / "a.txt", project / "new.txt"
    path = _write_jsonl(tmp_path / "events.jsonl",
        _cop_call("t1", "edit", {"path": str(a), "old_str": "beta", "new_str": "gamma"})
        + _cop_call("t2", "create", {"path": str(n), "file_text": "hi\nthere"})
        + _cop_call("t3", "edit", {"path": str(a), "old_str": "x", "new_str": "y"}, success=False)
        + _cop_call("t4", "view", {"path": str(a)}))
    found = changed_files(path, str(project), "copilot")
    by = _rows(found)
    assert sorted(by) == ["a.txt", "new.txt"]
    assert (by["a.txt"]["steps"], by["a.txt"]["additions"], by["a.txt"]["deletions"]) == (1, 1, 1)
    assert by["new.txt"]["additions"] == 2
    assert found["counts"] == _chat_edit_sum(path, "copilot")


# -------------------------------------------------------------------- Codex

_PATCH = (
    "*** Begin Patch\n"
    "*** Add File: src/new.py\n+one\n+two\n"
    "*** Update File: src/a.py\n@@ def f():\n ctx\n-old\n+new\n+more\n"
    "*** Delete File: src/gone.py\n"
    "*** End Patch"
)


def _codex(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "response_item", "timestamp": "2026-09-30T10:00:01Z", "payload": payload}


def _codex_patch(cid: str, patch: str, output: str = "Exit code: 0\nOutput:\nSuccess.") -> List[Dict[str, Any]]:
    return [_codex({"type": "custom_tool_call", "call_id": cid, "name": "apply_patch", "input": patch}),
            _codex({"type": "custom_tool_call_output", "call_id": cid, "output": output})]


def _codex_exec(cid: str, source: str, output: str = "ok") -> List[Dict[str, Any]]:
    return [_codex({"type": "custom_tool_call", "call_id": cid, "name": "exec", "input": source}),
            _codex({"type": "custom_tool_call_output", "call_id": cid,
                    "output": [{"type": "input_text", "text": output}]})]


class TestApplyPatchGrammar:

    def test_sections_become_wrote_edited_deleted(self):
        actions = parse_apply_patch(_PATCH)
        assert [(a["verb"], a["path"]) for a in actions] == [
            ("wrote", "src/new.py"), ("edited", "src/a.py"), ("deleted", "src/gone.py")]
        add, upd = actions[0], actions[1]
        assert (add["added"], add["removed"], add["created"]) == (2, 0, True)
        assert add["diff"]["numbered"] is True                       # an Add starts at line 1
        assert (upd["added"], upd["removed"]) == (2, 1)
        assert upd["diff"]["numbered"] is False and upd["diff"]["hunks"][0]["lines"] == [" ctx", "-old", "+new", "+more"]

    def test_a_move_edits_the_new_path_and_deletes_the_old(self):
        actions = parse_apply_patch(
            "*** Begin Patch\n*** Update File: a.py\n*** Move to: b.py\n@@\n-x\n+y\n*** End Patch")
        assert [(a["verb"], a["path"]) for a in actions] == [("edited", "b.py"), ("deleted", "a.py")]

    def test_an_incomplete_patch_reads_as_nothing(self):
        assert parse_apply_patch("*** Begin Patch\n*** Add File: a\n+x") == []
        assert parse_apply_patch("just text") == []

    def test_blank_context_lines_stay_in_the_hunk(self):
        (upd,) = parse_apply_patch("*** Begin Patch\n*** Update File: a\n@@\n a\n\n-b\n+c\n*** End Patch")
        assert upd["diff"]["hunks"][0]["lines"] == [" a", " ", "-b", "+c"]


class TestJsPatchLiterals:

    def test_escaped_double_quoted_literal(self):
        src = 'const patch = "*** Begin Patch\\n*** Add File: C:\\\\p\\\\a.txt\\n+x\\n*** End Patch";\ntools.apply_patch(patch);'
        (lit,) = _patch_literals(src)
        assert lit == "*** Begin Patch\n*** Add File: C:\\p\\a.txt\n+x\n*** End Patch"

    def test_template_and_raw_template_literals(self):
        body = "*** Begin Patch\n*** Add File: a\n+x\n*** End Patch"
        assert _patch_literals("const p = `\n" + body + "`;") == ["\n" + body]
        assert _patch_literals("const p = String.raw`" + body.replace("+x", "+a\\nb") + "`;") == [
            body.replace("+x", "+a\\nb")]

    def test_an_interpolated_template_is_skipped_not_guessed(self):
        assert _patch_literals("const p = `*** Begin Patch\n*** Add File: ${name}\n+x\n*** End Patch`;") == []

    def test_a_literal_without_an_end_marker_is_skipped(self):
        assert _patch_literals('const p = "*** Begin Patch\\n*** Add File: a\\n+x";') == []

    def test_unescape(self):
        assert _js_unescape(r"a\nb\t\"q\" \u0041 \x41 \\") == 'a\nb\t"q" A A \\'


class TestCodexFold:

    def _session(self, tmp_path: Path) -> Path:
        script = 'const patch = "*** Begin Patch\\n*** Update File: src/a.py\\n@@\\n-p\\n+q\\n*** End Patch";\nawait tools.apply_patch(patch);'
        rejected = 'const patch = "*** Begin Patch\\n*** Update File: src/a.py\\n@@\\n-r\\n+s\\n*** End Patch";\nawait tools.apply_patch(patch);'
        return _write_jsonl(tmp_path / "rollout.jsonl", [{"type": "session_meta", "payload": {"cwd": "x"}}]
            + _codex_patch("c1", _PATCH)
            + _codex_exec("c2", script)
            + _codex_patch("c3", "*** Begin Patch\n*** Update File: src/a.py\n@@\n-z\n+y\n*** Delete File: src/never.py\n*** End Patch",
                           "apply_patch verification failed: could not find lines")
            + _codex_exec("c4", rejected, "apply_patch verification failed: Failed to find expected lines")
            + _codex_exec("c5", "ls -la"))

    def test_direct_and_exec_patches_fold_per_file_with_relative_paths_resolved(self, tmp_path):
        project = tmp_path / "proj"
        (project / "src").mkdir(parents=True)
        (project / "src" / "a.py").write_text("x\n", encoding="utf-8")
        (project / "src" / "new.py").write_text("one\ntwo\n", encoding="utf-8")
        path = self._session(tmp_path)
        found = changed_files(path, str(project), "codex")
        by = _rows(found)
        # a rejected patch's Delete File (which records no diff) changed nothing either
        assert sorted(by) == ["src/a.py", "src/gone.py", "src/new.py"]
        assert by["src/new.py"]["status"] == "A"
        assert by["src/gone.py"]["status"] == "D"
        # the direct update (+2 -1) and the exec one (+1 -1); both rejected patches count nowhere
        assert (by["src/a.py"]["steps"], by["src/a.py"]["additions"], by["src/a.py"]["deletions"]) == (2, 3, 2)
        assert found["counts"]["additions"] == _chat_edit_sum(path, "codex")["additions"]
        assert found["counts"]["deletions"] == _chat_edit_sum(path, "codex")["deletions"]
        assert by["src/a.py"]["key"].replace("\\", "/") == (project / "src" / "a.py").as_posix()

    def test_a_file_opens_into_its_steps_in_order_and_a_delete_is_a_step(self, tmp_path):
        project = tmp_path / "proj"
        (project / "src").mkdir(parents=True)
        path = self._session(tmp_path)
        by = _rows(changed_files(path, str(project), "codex"))
        steps = file_steps(path, by["src/a.py"]["key"], flavor="codex", project_dir=str(project))
        assert [s["diff"]["hunks"][0]["lines"][-1] for s in steps["steps"]] == ["+more", "+q"]
        gone = file_steps(path, by["src/gone.py"]["key"], flavor="codex", project_dir=str(project))
        assert gone["steps"][0]["deleted"] is True
        new = file_steps(path, by["src/new.py"]["key"], flavor="codex", project_dir=str(project))
        assert new["steps"][0]["created"] is True and new["steps"][0]["diff"]["numbered"] is True

    def test_a_patch_step_is_marked_failed_in_chat_too(self, tmp_path):
        page = transcript_page(self._session(tmp_path), flavor="codex", limit=100)
        failed = [e for e in page["entries"] if e.get("error")]
        assert len(failed) == 5    # the rejected direct call + its two file steps, the rejected exec call + its one
        assert all("diff" not in (e.get("action") or {}) for e in failed)

    def test_codex_declares_partial_failure_recording(self, tmp_path):
        page = transcript_page(self._session(tmp_path), flavor="codex", limit=100)
        assert page["tool_errors"] == "partial"


# ------------------------------------------------------------------ bounding


def _big_codex(tmp_path: Path, count: int) -> Path:
    rows: List[Dict[str, Any]] = []
    for i in range(count):
        rows += _codex_patch(f"c{i}", f"*** Begin Patch\n*** Add File: f{i}.txt\n+{'x' * 200}\n*** End Patch")
    return _write_jsonl(tmp_path / "big.jsonl", rows)


def test_a_call_and_its_result_are_never_split_across_windows(tmp_path, monkeypatch):
    """With windows a few lines wide every call sits at some window's edge;
    the fold must still equal one pass over the whole file."""
    path = _big_codex(tmp_path, 40)
    whole = changed_files(path, None, "codex")
    monkeypatch.setattr(session_changes, "FLAVOR_WINDOW", 700)
    windowed = changed_files(path, None, "codex")
    assert len(whole["files"]) == 40 and windowed == whole


def test_a_failed_patch_in_the_next_window_still_cancels_its_call(tmp_path, monkeypatch):
    rows = _codex_patch("c1", "*** Begin Patch\n*** Add File: a.txt\n+x\n*** End Patch",
                        "apply_patch verification failed: nope")
    rows += _codex_exec("c2", "ls")
    path = _write_jsonl(tmp_path / "r.jsonl", rows)
    monkeypatch.setattr(session_changes, "FLAVOR_WINDOW", 1)
    assert changed_files(path, None, "codex")["files"] == []


def test_the_ceiling_keeps_the_newest_part_and_says_partial(tmp_path, monkeypatch):
    path = _big_codex(tmp_path, 40)
    monkeypatch.setattr(session_changes, "FLAVOR_SCAN_CEILING", path.stat().st_size // 2)
    found = changed_files(path, None, "codex")
    names = [r["path"] for r in found["files"]]
    assert found["partial"] is True and 0 < len(names) < 40
    assert "f39.txt" in names and "f0.txt" not in names           # the newest survive


def test_a_transcript_of_no_edits_is_an_empty_fold(tmp_path):
    path = _write_jsonl(tmp_path / "r.jsonl", _codex_exec("c1", "ls"))
    found = changed_files(path, None, "codex")
    assert found["files"] == [] and found["partial"] is False


# -------------------------------------------------------------------- routes


@pytest.fixture
def _bypass_gate(monkeypatch):
    from app.webapp import middleware
    monkeypatch.setattr(
        middleware, "LOOPBACK_HOSTS",
        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
    )


def _serve(overrides, monkeypatch, path: Path, project: Path, agent: str) -> None:
    overrides["session"].list_sessions.return_value = [{
        "session_id": "s1", "kind": "pty", "agent": agent, "alive": True,
        "project_dir": str(project), "started_at": 1_789_000_000,
    }]
    monkeypatch.setattr(board, "state_row_for_session",
                        lambda live, rows, sid: {"transcript_path": str(path)})


class TestRoutesServeEveryAgentWithAReader:

    def test_grok_session_lists_then_opens_a_file(self, webapp_client, _bypass_gate, monkeypatch, tmp_path):
        client, _, overrides = webapp_client
        project = tmp_path / "proj"
        project.mkdir()
        f = project / "f.txt"
        path = _write_jsonl(tmp_path / "updates.jsonl",
                            _grok_call("t1", "search_replace", {"file_path": str(f), "old_string": "a", "new_string": "b"}))
        _serve(overrides, monkeypatch, path, project, "grok")
        body = client.get("/api/claude-code/sessions/s1/changed-files").json()
        assert body["available"] is True and [r["path"] for r in body["files"]] == ["f.txt"]
        diff = client.get("/api/claude-code/sessions/s1/changed-files/diff", params={"path": body["files"][0]["key"]}).json()
        assert diff["available"] is True and diff["steps"][0]["diff"]["numbered"] is False

    def test_codex_relative_patch_paths_resolve_against_the_project(self, webapp_client, _bypass_gate, monkeypatch, tmp_path):
        client, _, overrides = webapp_client
        project = tmp_path / "proj"
        project.mkdir()
        path = _write_jsonl(tmp_path / "rollout.jsonl", _codex_patch("c1", _PATCH))
        _serve(overrides, monkeypatch, path, project, "codex")
        monkeypatch.setattr("app.webapp.routers.session_transcript.find_codex_transcript", lambda session: path)
        body = client.get("/api/claude-code/sessions/s1/changed-files").json()
        assert [r["path"] for r in body["files"]] == ["src/a.py", "src/gone.py", "src/new.py"]
        key = next(r["key"] for r in body["files"] if r["path"] == "src/a.py")
        diff = client.get("/api/claude-code/sessions/s1/changed-files/diff", params={"path": key}).json()
        assert diff["available"] is True and len(diff["steps"]) == 1

    def test_an_agent_without_a_reader_is_unsupported(self, webapp_client, _bypass_gate, monkeypatch, tmp_path):
        client, _, overrides = webapp_client
        _serve(overrides, monkeypatch, tmp_path / "x.jsonl", tmp_path, "aider")
        body = client.get("/api/claude-code/sessions/s1/changed-files").json()
        assert body["available"] is False and body["reason"] == "unsupported_agent"
        body = client.get("/api/claude-code/sessions/s1/changed-files/diff", params={"path": "x"}).json()
        assert body["available"] is False and body["reason"] == "unsupported_agent"
