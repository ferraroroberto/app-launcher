"""A session's Background tasks (#1955): launches and notifications folded
out of a Claude transcript, and the jail on a task's output file.

Every id, path and line of content here is synthetic.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from src.session_view import background_tasks as bt

SID = "11111111-2222-3333-4444-555555555555"
NOW = datetime(2026, 10, 1, 12, 10, 0, tzinfo=timezone.utc)


def _write(path: Path, rows: List[Dict[str, Any]]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def _launch(tool_use_id: str, name: str = "Bash", ts: str = "2026-10-01T12:00:00Z",
            **inputs: Any) -> Dict[str, Any]:
    inputs.setdefault("run_in_background", True)
    return {"type": "assistant", "timestamp": ts, "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tool_use_id, "name": name, "input": inputs}]}}


def _launched(tool_use_id: str, task_id: str, out: Optional[Path] = None,
              key: str = "backgroundTaskId", ts: str = "2026-10-01T12:00:01Z") -> Dict[str, Any]:
    text = "Command running in background with ID: %s." % task_id
    if out is not None:
        text += " Output is being written to: %s. You will be notified." % out
    return {"type": "user", "timestamp": ts, "toolUseResult": {key: task_id},
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id, "content": text}]}}


def _notice(task_id: str, status: Optional[str], summary: str = "done",
            ts: str = "2026-10-01T12:05:00Z", out: str = "", op: str = "enqueue") -> Dict[str, Any]:
    body = "<task-notification>\n<task-id>%s</task-id>\n" % task_id
    if out:
        body += "<output-file>%s</output-file>\n" % out
    if status:
        body += "<status>%s</status>\n" % status
    body += "<summary>%s</summary>\n</task-notification>" % summary
    return {"type": "queue-operation", "operation": op, "timestamp": ts, "content": body}


def _by_id(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {r["id"]: r for r in rows}


def test_launch_without_notification_is_running(tmp_path: Path) -> None:
    path = _write(tmp_path / (SID + ".jsonl"), [
        _launch("tu1", description="Serve the docs", command="python -m http.server"),
        _launched("tu1", "bAAA"),
    ])
    found = bt.background_tasks(path, NOW)
    assert [t["id"] for t in found["running"]] == ["bAAA"]
    assert found["finished"] == [] and found["finished_total"] == 0
    task = found["running"][0]
    assert task["status"] == "running"
    assert task["label"] == "Serve the docs"
    assert task["command"] == "python -m http.server"
    assert task["elapsed_seconds"] == 599  # from the launch result at 12:00:01 to now
    assert task["finished_at"] is None


def test_notification_with_status_finishes_the_task(tmp_path: Path) -> None:
    path = _write(tmp_path / (SID + ".jsonl"), [
        _launch("tu1", command="pytest -q\nmore"),
        _launched("tu1", "bAAA"),
        _notice("bAAA", "completed", 'Background command &quot;pytest&quot; completed (exit code 0)'),
    ])
    found = bt.background_tasks(path, NOW)
    assert found["running"] == []
    (task,) = found["finished"]
    assert task["status"] == "completed"
    assert task["summary"] == 'Background command "pytest" completed (exit code 0)'
    assert task["label"] == "pytest -q"          # no description: the command's first line
    assert task["elapsed_seconds"] == 299         # to the notification, not to now


def test_failed_and_killed_statuses_pass_through(tmp_path: Path) -> None:
    path = _write(tmp_path / (SID + ".jsonl"), [
        _launch("t1", description="a"), _launched("t1", "bAAA"),
        _launch("t2", description="b"), _launched("t2", "bBBB"),
        _notice("bAAA", "failed", "a failed (exit code 2)", ts="2026-10-01T12:03:00Z"),
        _notice("bBBB", "killed", "b was stopped", ts="2026-10-01T12:04:00Z"),
    ])
    finished = _by_id(bt.background_tasks(path, NOW)["finished"])
    assert finished["bAAA"]["status"] == "failed"
    assert finished["bBBB"]["status"] == "killed"


def test_monitor_events_keep_it_running_until_a_status(tmp_path: Path) -> None:
    path = _write(tmp_path / (SID + ".jsonl"), [
        _launch("tm", name="Monitor", description="Watch the build", command="tail -f log",
                run_in_background=None),
        _launched("tm", "mAAA", key="taskId"),
        _notice("mAAA", None, "event line 1", ts="2026-10-01T12:02:00Z"),
        _notice("mAAA", None, "event line 2", ts="2026-10-01T12:03:00Z"),
    ])
    found = bt.background_tasks(path, NOW)
    assert [t["id"] for t in found["running"]] == ["mAAA"]
    assert found["running"][0]["kind"] == "monitor"
    _write(path, [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()] + [
        _notice("mAAA", "completed", "stream ended", ts="2026-10-01T12:06:00Z")])
    found = bt.background_tasks(path, NOW)
    assert found["running"] == [] and found["finished"][0]["status"] == "completed"


def test_duplicate_notification_lines_count_once(tmp_path: Path) -> None:
    path = _write(tmp_path / (SID + ".jsonl"), [
        _launch("tu1", description="x"), _launched("tu1", "bAAA"),
        _notice("bAAA", "completed", "x done", ts="2026-10-01T12:04:00Z"),
        _notice("bAAA", "completed", "x done", ts="2026-10-01T12:09:00Z", op="remove"),
        {"type": "attachment", "timestamp": "2026-10-01T12:09:30Z", "attachment": {
            "type": "queued_command",
            "prompt": _notice("bAAA", "completed", "x done")["content"]}},
    ])
    found = bt.background_tasks(path, NOW)
    assert found["finished_total"] == 1
    assert found["finished"][0]["finished_at"] == "2026-10-01T12:04:00Z"


def test_a_notice_for_an_unseen_launch_is_listed_as_finished(tmp_path: Path) -> None:
    path = _write(tmp_path / (SID + ".jsonl"), [_notice("bOLD", "completed", "an older job ended")])
    (task,) = bt.background_tasks(path, NOW)["finished"]
    assert task["label"] == "an older job ended"
    assert task["elapsed_seconds"] is None


def test_no_tasks(tmp_path: Path) -> None:
    path = _write(tmp_path / (SID + ".jsonl"), [
        {"type": "user", "timestamp": "2026-10-01T12:00:00Z",
         "message": {"role": "user", "content": "hello"}},
        _launch("tu1", run_in_background=None, command="ls"),   # a foreground call
    ])
    assert bt.background_tasks(path, NOW) == {"running": [], "finished": [], "finished_total": 0}


def test_system_prompt_lines_are_never_parsed(tmp_path: Path) -> None:
    path = _write(tmp_path / (SID + ".jsonl"), [
        {"type": "attachment", "attachment": {"type": "prompt_snapshot",
         "systemPrompt": ["run_in_background Monitor <task-notification><task-id>zzz</task-id></task-notification>"]}},
    ])
    assert bt.background_tasks(path, NOW)["finished_total"] == 0


def test_finished_list_is_capped_newest_first(tmp_path: Path) -> None:
    rows: List[Dict[str, Any]] = []
    for i in range(bt.FINISHED_LIMIT + 5):
        rows += [_launch("tu%d" % i, description="job %d" % i), _launched("tu%d" % i, "b%03d" % i),
                 _notice("b%03d" % i, "completed", "ok", ts="2026-10-01T12:%02d:00Z" % (i % 60))]
    found = bt.background_tasks(_write(tmp_path / (SID + ".jsonl"), rows), NOW)
    assert len(found["finished"]) == bt.FINISHED_LIMIT
    assert found["finished_total"] == bt.FINISHED_LIMIT + 5
    stamps = [t["finished_at"] for t in found["finished"]]
    assert stamps == sorted(stamps, reverse=True)


# ------------------------------------------------------------ output jail


def _output(root: Path, session: str = SID, task: str = "bAAA", body: str = "line\n") -> Path:
    folder = root / "claude" / "proj" / session / "tasks"
    folder.mkdir(parents=True, exist_ok=True)
    file = folder / (task + ".output")
    file.write_text(body, encoding="utf-8")
    return file


def _session_with_output(tmp_path: Path, out: Path) -> Path:
    return _write(tmp_path / (SID + ".jsonl"), [
        _launch("tu1", description="x"), _launched("tu1", "bAAA", out)])


def test_output_tail_of_the_sessions_own_file(tmp_path: Path) -> None:
    temp = tmp_path / "temp"
    out = _output(temp, body="\x1b[32mgreen\x1b[0m\nplain\n")
    path = _session_with_output(tmp_path, out)
    found = bt.background_tasks(path, NOW, temp_root=temp / "claude")
    assert found["running"][0]["has_output"] is True
    tail = bt.task_output(path, "bAAA", temp_root=temp / "claude")
    assert tail == {"text": "green\nplain\n", "size": out.stat().st_size, "truncated": False}


def test_output_tail_is_capped_from_a_whole_line(tmp_path: Path) -> None:
    temp = tmp_path / "temp"
    body = "".join("row %05d\n" % i for i in range(3000))
    out = _output(temp, body=body)
    tail = bt.task_output(_session_with_output(tmp_path, out), "bAAA", temp_root=temp / "claude")
    assert tail is not None and tail["truncated"] is True
    assert len(tail["text"].encode()) <= bt.OUTPUT_TAIL_BYTES
    assert tail["text"].startswith("row ") and tail["text"].endswith("row 02999\n")


@pytest.mark.parametrize("make", ["other_session", "other_task", "outside_root", "wrong_folder"])
def test_output_outside_the_sessions_own_files_is_refused(tmp_path: Path, make: str) -> None:
    temp = tmp_path / "temp"
    own = _output(temp)                                  # the legitimate file exists too
    if make == "other_session":
        target = _output(temp, session="99999999-0000-0000-0000-000000000000")
    elif make == "other_task":
        target = _output(temp, task="bZZZ")
    elif make == "outside_root":
        target = tmp_path / "secret.output"
        target.write_text("secret", encoding="utf-8")
    else:
        target = temp / "claude" / "proj" / SID / "notes" / "bAAA.output"
        target.parent.mkdir(parents=True)
        target.write_text("secret", encoding="utf-8")
    assert own.is_file()
    path = _session_with_output(tmp_path, target)
    assert bt.task_output(path, "bAAA", temp_root=temp / "claude") is None
    assert bt.background_tasks(path, NOW, temp_root=temp / "claude")["running"][0]["has_output"] is False


def test_output_through_a_link_is_refused(tmp_path: Path) -> None:
    temp = tmp_path / "temp"
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    folder = temp / "claude" / "proj" / SID / "tasks"
    folder.mkdir(parents=True)
    link = folder / "bAAA.output"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("no symlink privilege on this host")
    assert bt.task_output(_session_with_output(tmp_path, link), "bAAA", temp_root=temp / "claude") is None


def test_unknown_task_and_missing_file(tmp_path: Path) -> None:
    temp = tmp_path / "temp"
    path = _session_with_output(tmp_path, temp / "claude" / "proj" / SID / "tasks" / "bAAA.output")
    assert bt.task_output(path, "bAAA", temp_root=temp / "claude") is None      # file never written
    assert bt.task_output(path, "bNOPE", temp_root=temp / "claude") is None     # no such task


# ------------------------------------------------------------------ route

_ROUTE = "/api/claude-code/sessions/s1/background-tasks"


@pytest.fixture
def _bypass_gate(monkeypatch):
    from app.webapp import middleware
    monkeypatch.setattr(
        middleware.trust, "LOOPBACK_HOSTS",
        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
    )


def _serve(overrides, monkeypatch, path: Path, agent: str = "claude") -> None:
    from src import board
    overrides["session"].list_sessions.return_value = [{
        "session_id": "s1", "kind": "pty", "agent": agent, "alive": True,
        "project_dir": str(path.parent), "started_at": 1_789_000_000,
    }]
    monkeypatch.setattr(board, "state_row_for_session",
                        lambda live, rows, sid: {"transcript_path": str(path)})


def test_route_is_classified_passkey_and_refused_off_tailnet(webapp_client):
    from app.webapp.middleware import _terminal_guard_level
    assert _terminal_guard_level("/api/claude-code/sessions/abc/background-tasks") == "passkey"
    client, _, _ = webapp_client
    assert client.get(_ROUTE).status_code == 403


class TestBackgroundTasksRoute:

    def test_lists_then_reads_one_output(self, webapp_client, _bypass_gate, monkeypatch, tmp_path, caplog):
        import logging
        import tempfile
        client, _, overrides = webapp_client
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "temp"))
        out = _output(tmp_path / "temp", body="secret-output-text\n")
        path = _write(tmp_path / (SID + ".jsonl"), [
            _launch("tu1", description="Serve"), _launched("tu1", "bAAA", out)])
        _serve(overrides, monkeypatch, path)
        with caplog.at_level(logging.INFO):
            body = client.get(_ROUTE).json()
            tail = client.get(_ROUTE, params={"task": "bAAA"}).json()
        assert body["available"] is True and [t["id"] for t in body["running"]] == ["bAAA"]
        assert body["running"][0]["has_output"] is True
        assert tail["available"] is True and tail["text"] == "secret-output-text\n"
        joined = "\n".join(r.getMessage() for r in caplog.records if "background task" in r.getMessage())
        assert "background tasks s1" in joined and "secret-output-text" not in joined

    def test_a_task_the_session_never_ran_has_no_output(self, webapp_client, _bypass_gate, monkeypatch, tmp_path):
        client, _, overrides = webapp_client
        _serve(overrides, monkeypatch, _write(tmp_path / (SID + ".jsonl"), []))
        body = client.get(_ROUTE, params={"task": "bOTHER"}).json()
        assert body["available"] is False and body["reason"] == "output_not_found"

    def test_only_claude_has_background_tasks(self, webapp_client, _bypass_gate, monkeypatch, tmp_path):
        client, _, overrides = webapp_client
        _serve(overrides, monkeypatch, _write(tmp_path / (SID + ".jsonl"), []), agent="codex")
        body = client.get(_ROUTE).json()
        assert body["available"] is False and body["reason"] == "unsupported_agent"

    def test_no_transcript_is_said(self, webapp_client, _bypass_gate, monkeypatch, tmp_path):
        client, _, overrides = webapp_client
        _serve(overrides, monkeypatch, tmp_path / "missing.jsonl")
        body = client.get(_ROUTE).json()
        assert body["available"] is False and body["reason"] == "no_transcript"

    def test_an_empty_session_lists_nothing(self, webapp_client, _bypass_gate, monkeypatch, tmp_path):
        client, _, overrides = webapp_client
        _serve(overrides, monkeypatch, _write(tmp_path / (SID + ".jsonl"), []))
        body = client.get(_ROUTE).json()
        assert body["available"] is True and body["running"] == [] and body["finished_total"] == 0
