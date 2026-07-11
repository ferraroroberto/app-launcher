"""Board drill-down (issue #301) — exchange parser, reply proxy, issue start.

Covers the act-from-the-card loop server-side:
  * ``board.last_exchange`` — tail JSONL parsing: text blocks joined across
    lines of the same assistant message, thinking/tool_use lines skipped,
    tool-result user lines skipped, harness wrappers skipped, missing file
    degraded to ``available: False``.
  * ``board.state_row_for_session`` — resolves the same row the board's
    merge renders (newest-session-wins claim order).
  * ``POST /api/claude-code/sessions/{sid}/input`` — bracketed-paste framing
    for multi-line data and the two-write CR rule (#166).
  * ``POST /api/board/issues/start`` — server-built ``/issue-<mode> <N>``
    prompt, mode/number validation, repo resolution in the projects folder.
  * ``GET /api/board/sessions/{sid}/exchange`` — cwd-join to the state row's
    ``transcript_path``.
  * passkey classification of all three new paths (gate refusal off-tailnet
    + ``_terminal_guard_level`` mapping).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import board


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------ last_exchange


def _write_jsonl(path: Path, lines: list) -> str:
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )
    return str(path)


def _user_line(text) -> dict:
    return {
        "type": "user", "timestamp": "2026-07-02T11:50:00Z",
        "message": {"role": "user", "content": text},
    }


def _assistant_line(blocks: list, msg_id: str = "m1") -> dict:
    return {
        "type": "assistant", "timestamp": "2026-07-02T11:55:00Z",
        "message": {"id": msg_id, "role": "assistant", "content": blocks},
    }


def test_last_exchange_happy_path(tmp_path: Path):
    target = _write_jsonl(tmp_path / "t.jsonl", [
        _user_line("fix the bug please"),
        _assistant_line([{"type": "thinking", "thinking": "hmm"}]),
        _assistant_line([{"type": "tool_use", "name": "Bash", "input": {}}]),
        _user_line([{"type": "tool_result", "content": "exit 0"}]),
        _assistant_line([{"type": "text", "text": "Done — the bug is fixed."}]),
    ])
    result = board.last_exchange(target)
    assert result["available"] is True
    assert result["assistant"]["text"] == "Done — the bug is fixed."
    assert result["user"]["text"] == "fix the bug please"


def test_last_exchange_joins_blocks_of_same_message(tmp_path: Path):
    """Transcripts write one line per content block — same message.id lines
    are one reply and must be joined in order."""
    target = _write_jsonl(tmp_path / "t.jsonl", [
        _user_line("q"),
        _assistant_line([{"type": "text", "text": "First part."}], msg_id="m9"),
        _assistant_line([{"type": "tool_use", "name": "Read"}], msg_id="m9"),
        _assistant_line([{"type": "text", "text": "Second part."}], msg_id="m9"),
    ])
    result = board.last_exchange(target)
    assert result["assistant"]["text"] == "First part.\n\nSecond part."


def test_last_exchange_earlier_message_not_merged(tmp_path: Path):
    target = _write_jsonl(tmp_path / "t.jsonl", [
        _user_line("q"),
        _assistant_line([{"type": "text", "text": "Old reply."}], msg_id="m1"),
        _user_line("follow-up"),
        _assistant_line([{"type": "text", "text": "New reply."}], msg_id="m2"),
    ])
    result = board.last_exchange(target)
    assert result["assistant"]["text"] == "New reply."
    assert result["user"]["text"] == "follow-up"


def test_last_exchange_skips_harness_wrapper_user_lines(tmp_path: Path):
    target = _write_jsonl(tmp_path / "t.jsonl", [
        _user_line("the real prompt"),
        _user_line("<command-name>/compact</command-name>"),
        _assistant_line([{"type": "text", "text": "reply"}]),
    ])
    assert board.last_exchange(target)["user"]["text"] == "the real prompt"


def test_last_exchange_missing_or_empty():
    assert board.last_exchange(None)["available"] is False
    assert board.last_exchange("C:/nope/missing.jsonl")["available"] is False


def test_last_exchange_no_assistant_text_in_tail(tmp_path: Path):
    target = _write_jsonl(tmp_path / "t.jsonl", [
        _user_line("q"),
        _assistant_line([{"type": "tool_use", "name": "Bash"}]),
    ])
    assert board.last_exchange(target)["available"] is False


# --------------------------------------------------- state_row_for_session


def _live_sess(session_id: str, project_dir: str, started_min_ago: int) -> dict:
    return {
        "session_id": session_id,
        "kind": "pty",
        "alive": True,
        "project_dir": project_dir,
        "started_at": _iso(NOW - timedelta(minutes=started_min_ago)),
    }


def test_state_row_for_session_matches_render_claim():
    live = [_live_sess("old", "E:/a/x", 120), _live_sess("new", "E:/a/x", 5)]
    rows = {
        "t1": {"cwd": "E:/a/x", "status": "needs-you",
               "updated_at": _iso(NOW - timedelta(minutes=1)),
               "transcript_path": "p1"},
    }
    assert board.state_row_for_session(live, rows, "new")["transcript_path"] == "p1"
    assert board.state_row_for_session(live, rows, "old") is None
    assert board.state_row_for_session(live, rows, "ghost") is None


# ----------------------------------------------------------- passkey gates


def test_new_paths_classified_passkey():
    from app.webapp.middleware import _terminal_guard_level
    assert _terminal_guard_level("/api/claude-code/sessions/abc/input") == "passkey"
    assert _terminal_guard_level("/api/board/sessions/abc/exchange") == "passkey"
    assert _terminal_guard_level("/api/board/issues/start") == "passkey"


class TestGateRefusal:
    """The TestClient connects as host 'testclient' (not loopback, not
    tailnet) — all three #301 endpoints must be refused outright."""

    def test_input_refused_off_tailnet(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/claude-code/sessions/s1/input", json={"data": "hi"}
        )
        assert resp.status_code == 403

    def test_exchange_refused_off_tailnet(self, webapp_client):
        client, _, _ = webapp_client
        assert client.get("/api/board/sessions/s1/exchange").status_code == 403

    def test_issue_start_refused_off_tailnet(self, webapp_client):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/board/issues/start",
            json={"repo": "x", "number": 1, "mode": "start"},
        )
        assert resp.status_code == 403


@pytest.fixture
def _bypass_gate(monkeypatch):
    """Treat the TestClient host as loopback so the gated proxy logic is
    exercised (the gate itself is covered by TestGateRefusal)."""
    from app.webapp import middleware
    monkeypatch.setattr(
        middleware,
        "LOOPBACK_HOSTS",
        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
    )


# ------------------------------------------------------------- reply proxy


class TestInputProxy:

    def test_multiline_is_bracketed_and_cr_is_second_write(
        self, webapp_client, _bypass_gate
    ):
        client, _, overrides = webapp_client
        resp = client.post(
            "/api/claude-code/sessions/s1/input",
            json={"data": "line one\nline two", "submit": True},
        )
        assert resp.status_code == 200
        calls = overrides["session"].send_input.call_args_list
        assert len(calls) == 2
        assert calls[0].args == (8446, "s1", "\x1b[200~line one\nline two\x1b[201~")
        assert calls[1].args == (8446, "s1", "\r")

    def test_single_line_not_bracketed(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        client.post(
            "/api/claude-code/sessions/s1/input",
            json={"data": "hello", "submit": True},
        )
        calls = overrides["session"].send_input.call_args_list
        assert calls[0].args == (8446, "s1", "hello")
        assert calls[1].args == (8446, "s1", "\r")

    def test_no_submit_sends_no_cr(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        client.post(
            "/api/claude-code/sessions/s1/input",
            json={"data": "draft", "submit": False},
        )
        assert len(overrides["session"].send_input.call_args_list) == 1

    def test_empty_data_is_400(self, webapp_client, _bypass_gate):
        client, _, _ = webapp_client
        assert client.post(
            "/api/claude-code/sessions/s1/input", json={"data": "   "}
        ).status_code == 400


# ------------------------------------------------------------- issue start


class TestIssueStart:

    @pytest.fixture
    def _spawn(self, webapp_client, monkeypatch):
        from app.webapp.routers import board as board_router
        captured: dict = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent, rows, cols,
                       history_lines=None):
            captured.update(
                project_dir=project_dir, name=name, flags=flags,
                port=port, kind=kind, agent=agent, rows=rows, cols=cols,
                history_lines=history_lines,
            )
            return {"session_id": "spawned-1", "kind": "pty", "name": name}

        monkeypatch.setattr(board_router, "spawn_claude_session", fake_spawn)
        return captured

    def test_builds_server_side_prompt(self, webapp_client, _bypass_gate, _spawn):
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        resp = client.post(
            "/api/board/issues/start",
            json={"repo": "MyRepo", "number": 42, "mode": "start"},
        )
        assert resp.status_code == 200
        assert _spawn["flags"].endswith(' "/issue-start 42"')
        assert Path(_spawn["project_dir"]).name == "myrepo"
        assert _spawn["kind"] == "pty" and _spawn["agent"] == "claude"
        assert resp.json()["session"]["session_id"] == "spawned-1"

    def test_yolo_mode(self, webapp_client, _bypass_gate, _spawn):
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        client.post(
            "/api/board/issues/start",
            json={"repo": "myrepo", "number": 7, "mode": "yolo"},
        )
        assert _spawn["flags"].endswith(' "/issue-yolo 7"')

    def test_rejects_bad_mode_and_number(self, webapp_client, _bypass_gate, _spawn):
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        base = {"repo": "myrepo", "number": 1, "mode": "start"}
        assert client.post(
            "/api/board/issues/start", json={**base, "mode": "add; rm -rf"}
        ).status_code == 400
        assert client.post(
            "/api/board/issues/start", json={**base, "number": "abc"}
        ).status_code == 400
        assert client.post(
            "/api/board/issues/start", json={**base, "number": -3}
        ).status_code == 400

    def test_unknown_repo_is_404(self, webapp_client, _bypass_gate, _spawn):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/board/issues/start",
            json={"repo": "not-checked-out", "number": 1, "mode": "start"},
        )
        assert resp.status_code == 404


# -------------------------------------------------------- exchange endpoint


class TestExchangeEndpoint:

    def test_resolves_transcript_via_state_row(
        self, webapp_client, _bypass_gate, tmp_path: Path
    ):
        client, app, overrides = webapp_client
        transcript = _write_jsonl(tmp_path / "t.jsonl", [
            _user_line("status?"),
            _assistant_line([{"type": "text", "text": "All green."}]),
        ])
        state_file = Path(app.state.webapp_config.sessions_state_file)
        state_file.write_text(json.dumps({
            "t-uuid": {"cwd": "E:/proj/app", "status": "needs-you",
                       "updated_at": _iso(NOW), "transcript_path": transcript},
        }), encoding="utf-8")
        overrides["session"].list_sessions.return_value = [
            _live_sess("sess1", "E:/proj/app", 10)
        ]
        body = client.get("/api/board/sessions/sess1/exchange").json()
        assert body["available"] is True
        assert body["assistant"]["text"] == "All green."
        assert body["user"]["text"] == "status?"

    def test_unknown_session_degrades(self, webapp_client, _bypass_gate):
        client, _, _ = webapp_client
        body = client.get("/api/board/sessions/ghost/exchange").json()
        assert body == {"available": False, "user": None, "assistant": None}
