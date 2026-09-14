"""Coding-tab session transcript (issue #953) — parser, pagination, router.

  * ``session_transcript.claude_entries`` — typed entries from hook JSONL:
    assistant text merged across lines of one ``message.id``, tool results
    paired to their call, harness wrappers / ``isMeta`` bodies / ``system``
    rows typed as folded ``system``, image-paste prompts typed as ``user``,
    sidechain lines flagged, metadata rows dropped, display caps.
  * ``session_transcript.transcript_page`` — bounded backwards paging: a
    torn line at a window edge is never duplicated or dropped, a record wider
    than the window is read whole, the per-request byte cap yields a short
    page with a cursor, and the concatenation of every page equals one full
    parse.
  * ``session_transcript.codex_entries`` — the rollout grammar.
  * ``GET /api/claude-code/sessions/{sid}/transcript`` — passkey-gated,
    distinct unavailable reasons, no transcript bodies in the log.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src import board, session_transcript as st


# ------------------------------------------------------------ fixtures


def _write_jsonl(path: Path, lines: List[Dict[str, Any]]) -> Path:
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )
    return path


def _user(text: Any, **extra: Any) -> Dict[str, Any]:
    row = {"type": "user", "timestamp": "2026-09-14T10:00:00Z",
           "message": {"role": "user", "content": text}}
    row.update(extra)
    return row


def _assistant(blocks: List[Dict[str, Any]], msg_id: str = "m1", **extra: Any) -> Dict[str, Any]:
    row = {"type": "assistant", "timestamp": "2026-09-14T10:00:01Z",
           "message": {"id": msg_id, "role": "assistant", "content": blocks}}
    row.update(extra)
    return row


def _tool_use(name: str, inputs: Dict[str, Any], tid: str) -> Dict[str, Any]:
    return {"type": "tool_use", "id": tid, "name": name, "input": inputs}


def _tool_result(tid: str, content: Any) -> Dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": tid, "content": content}


def _parse_whole(path: Path, build=st.claude_entries) -> List[Dict[str, Any]]:
    """Reference: one parse of the whole file, offsets stripped."""
    raw = path.read_bytes()
    lines, pos = [], 0
    for chunk in raw.split(b"\n"):
        if chunk.strip():
            lines.append((pos, chunk.decode("utf-8", errors="replace")))
        pos += len(chunk) + 1
    entries = build(lines)
    for e in entries:
        e.pop("offset", None)
    return entries


def _walk(path: Path, limit: int, flavor: str = "claude") -> List[List[Dict[str, Any]]]:
    """Every page from newest to oldest, following ``next_cursor``."""
    pages, cursor, guard = [], None, 0
    while True:
        page = st.transcript_page(path, before=cursor, limit=limit, flavor=flavor)
        pages.append(page["entries"])
        cursor = page["next_cursor"]
        guard += 1
        assert guard < 10_000, "cursor never reached the file start"
        if cursor is None:
            return pages


def _kinds(entries: List[Dict[str, Any]]) -> List[str]:
    return [e["kind"] for e in entries]


# -------------------------------------------------------- claude_entries


def test_claude_entries_types_every_shape(tmp_path: Path):
    path = _write_jsonl(tmp_path / "t.jsonl", [
        {"type": "mode", "mode": "normal"},                       # metadata: dropped
        _user("<command-name>/issue-start</command-name>"),      # harness wrapper
        _user("Base directory for this skill: …", isMeta=True),  # injected skill body
        _user("<system-reminder>ctx</system-reminder>"),
        _user("Please fix the bug"),
        _assistant([{"type": "thinking", "thinking": "hmm"}], "m1"),
        _assistant([{"type": "text", "text": "On it."}], "m1"),
        _assistant([_tool_use("Bash", {"command": "pytest -q", "timeout": 5}, "t1")], "m1"),
        _user([_tool_result("t1", "3 passed")]),
        _assistant([{"type": "text", "text": "All green."}], "m1"),
        _assistant([{"type": "text", "text": "Sub-agent says hi"}], "m9", isSidechain=True),
        {"type": "system", "subtype": "compact_boundary", "content": "Conversation compacted",
         "timestamp": "2026-09-14T10:00:05Z"},
        {"type": "system", "subtype": "turn_duration", "durationMs": 12},  # no content: dropped
        _user([{"type": "text", "text": "Look at this"}, {"type": "image", "source": {}}]),
        _assistant([{"type": "text", "text": "Nice screenshot."}], "m2"),
    ])
    entries = _parse_whole(path)
    assert _kinds(entries) == [
        "system", "system", "system", "user", "thinking", "assistant",
        "tool_call", "assistant", "system", "user", "assistant",
    ]
    system = [e for e in entries if e["kind"] == "system"]
    assert [e["label"] for e in system] == ["command", "injected", "system-reminder", "compact_boundary"]
    assert entries[3]["text"] == "Please fix the bug"
    # Both text blocks of m1 merge into one entry, in place, with the
    # tool call between them staying where it was written.
    assistant_m1 = entries[5]
    assert assistant_m1["text"] == "On it.\n\nAll green."
    call = entries[6]
    assert call["name"] == "Bash"
    assert call["summary"] == "pytest -q"          # first string-valued input
    assert call["result"] == "3 passed"
    assert call["result_truncated"] is False
    sidechain = entries[7]
    assert sidechain["sidechain"] is True and sidechain["text"] == "Sub-agent says hi"
    assert entries[9]["text"] == "Look at this\n\n[image]"
    assert all(e["timestamp"] for e in entries)


def test_claude_entries_caps_long_bodies(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(st, "ASSISTANT_TEXT_CAP", 20)
    monkeypatch.setattr(st, "TOOL_RESULT_CAP", 5)
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _assistant([{"type": "text", "text": "x" * 15}], "m1"),
        _assistant([{"type": "text", "text": "y" * 15}], "m1"),
        _assistant([_tool_use("Read", {"file_path": "a.py"}, "t1")], "m1"),
        _user([_tool_result("t1", [{"type": "text", "text": "0123456789"}])]),
    ])
    entries = _parse_whole(path)
    assert entries[0]["truncated"] is True and len(entries[0]["text"]) == 20
    assert entries[1]["result"] == "01234" and entries[1]["result_truncated"] is True


def test_unknown_tool_result_stands_alone(tmp_path: Path):
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _user([_tool_result("ghost", "orphan output")]),
    ])
    entries = _parse_whole(path)
    assert _kinds(entries) == ["tool_result"]
    assert entries[0]["tool_use_id"] == "ghost" and entries[0]["text"] == "orphan output"


# ------------------------------------------------------- transcript_page


def _conversation(turns: int, *, tool_calls_per_turn: int = 2) -> List[Dict[str, Any]]:
    lines: List[Dict[str, Any]] = []
    for i in range(turns):
        lines.append(_user(f"prompt {i} " + "p" * (i % 7)))
        mid = f"m{i}"
        lines.append(_assistant([{"type": "thinking", "thinking": f"think {i}"}], mid))
        lines.append(_assistant([{"type": "text", "text": f"reply {i} opener"}], mid))
        for k in range(tool_calls_per_turn):
            tid = f"t{i}-{k}"
            lines.append(_assistant([_tool_use("Bash", {"command": f"cmd {i} {k}"}, tid)], mid))
            lines.append(_user([_tool_result(tid, "out " + "o" * ((i * k) % 50))]))
        lines.append(_assistant([{"type": "text", "text": f"reply {i} closer " + "r" * (i % 11)}], mid))
    return lines


@pytest.mark.parametrize("limit", [1, 7, 40])
def test_pages_concatenate_to_one_full_parse(tmp_path: Path, monkeypatch, limit: int):
    """No turn duplicated or dropped across pages, whatever the page size —
    with the read window shrunk so every window edge tears a line."""
    monkeypatch.setattr(st, "WINDOW_BYTES", 300)
    path = _write_jsonl(tmp_path / "t.jsonl", _conversation(60))
    pages = _walk(path, limit)
    assert len(pages) > 1
    for page in pages:
        assert sum(1 for e in page if e["kind"] in ("user", "assistant")) <= limit
    joined = [e for page in reversed(pages) for e in page]
    assert joined == _parse_whole(path)


def test_first_page_is_the_newest_turns_and_cursor_advances(tmp_path: Path):
    """``limit`` counts user + assistant entries — two per exchange here."""
    path = _write_jsonl(tmp_path / "t.jsonl", _conversation(10))
    page = st.transcript_page(path, limit=4)
    users = [e["text"] for e in page["entries"] if e["kind"] == "user"]
    assert users == ["prompt 8 p", "prompt 9 pp"]
    # The exchange's thinking + tool blocks ride with their message.
    assert _kinds(page["entries"])[:4] == ["user", "thinking", "assistant", "tool_call"]
    assert page["next_cursor"] is not None
    older = st.transcript_page(path, before=page["next_cursor"], limit=4)
    assert [e["text"] for e in older["entries"] if e["kind"] == "user"] == ["prompt 6 pppppp", "prompt 7"]


def test_whole_file_in_one_page_has_no_cursor(tmp_path: Path):
    path = _write_jsonl(tmp_path / "t.jsonl", _conversation(3))
    page = st.transcript_page(path, limit=40)
    assert page["next_cursor"] is None
    assert page["entries"] == _parse_whole(path)


def test_record_wider_than_the_window_is_read_whole(tmp_path: Path, monkeypatch):
    """The smoke-test regression: an image-paste prompt is one JSONL line of
    hundreds of KB. Sliding the window into its middle used to tear its tail
    off and lose the whole turn."""
    monkeypatch.setattr(st, "WINDOW_BYTES", 200)
    big = "B" * 5_000
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _user("first"),
        _assistant([{"type": "text", "text": "ok"}], "m0"),
        _user(big),
        _assistant([{"type": "text", "text": "seen it"}], "m1"),
        _user("last"),
        _assistant([{"type": "text", "text": "bye"}], "m2"),
    ])
    pages = _walk(path, 1)
    joined = [e for page in reversed(pages) for e in page]
    assert [e["text"][:5] for e in joined if e["kind"] == "user"] == ["first", "BBBBB", "last"]
    assert joined == _parse_whole(path)


def test_byte_cap_yields_a_short_page_with_a_cursor(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(st, "WINDOW_BYTES", 300)
    monkeypatch.setattr(st, "REQUEST_BYTE_CAP", 1_200)
    path = _write_jsonl(tmp_path / "t.jsonl", _conversation(40))
    page = st.transcript_page(path, limit=100)
    turns = sum(1 for e in page["entries"] if e["kind"] in ("user", "assistant"))
    assert 0 < turns < 80, "the cap must cut the page well short of the 100 asked"
    assert page["next_cursor"] is not None
    # …and the walk still completes losslessly.
    pages = _walk(path, 100)
    assert [e for page in reversed(pages) for e in page] == _parse_whole(path)


def test_pair_split_across_a_page_boundary_keeps_the_result(tmp_path: Path):
    """A tool call whose result lands on the newer page: the result stands
    alone there (never lost), the call shows no result on the older page."""
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _user("go"),
        _assistant([_tool_use("Bash", {"command": "ls"}, "t1")], "m1"),
        _assistant([{"type": "text", "text": "meanwhile"}], "m2"),
        _user([_tool_result("t1", "a b c")]),
    ])
    newest = st.transcript_page(path, limit=1)
    assert _kinds(newest["entries"]) == ["assistant", "tool_result"]
    assert newest["entries"][1]["text"] == "a b c"
    older = st.transcript_page(path, before=newest["next_cursor"], limit=1)
    assert _kinds(older["entries"]) == ["user", "tool_call"]
    assert older["entries"][1]["result"] is None
    assert older["next_cursor"] is None


def test_missing_file_raises_oserror(tmp_path: Path):
    with pytest.raises(OSError):
        st.transcript_page(tmp_path / "nope.jsonl")


def test_empty_file_is_an_empty_page(tmp_path: Path):
    path = tmp_path / "empty.jsonl"
    path.write_bytes(b"")
    assert st.transcript_page(path) == {"entries": [], "next_cursor": None}


# --------------------------------------------------------- codex_entries


def _codex(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "response_item", "timestamp": "2026-09-08T15:49:10Z", "payload": payload}


def test_codex_entries_grammar(tmp_path: Path):
    path = _write_jsonl(tmp_path / "rollout.jsonl", [
        {"type": "session_meta", "payload": {"cwd": "E:/x"}},
        _codex({"type": "message", "role": "developer",
                "content": [{"type": "input_text", "text": "<skills_instructions>…"}]}),
        _codex({"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "<environment_context>cwd</environment_context>"}]}),
        _codex({"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "review #877"}]}),
        _codex({"type": "reasoning", "summary": [{"type": "summary_text", "text": "plan it"}]}),
        _codex({"type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "Inspecting."}]}),
        _codex({"type": "custom_tool_call", "call_id": "c1", "name": "exec", "input": "Get-Content x"}),
        _codex({"type": "custom_tool_call_output", "call_id": "c1",
                "output": [{"type": "input_text", "text": "done"}]}),
        _codex({"type": "function_call", "call_id": "c2", "name": "shell", "arguments": "{\"cmd\": \"ls\"}"}),
        _codex({"type": "function_call_output", "call_id": "c2", "output": "a\nb"}),
        _codex({"type": "agent_message", "author": "/root", "content": [{"type": "input_text", "text": "NEW_TASK"}]}),
        {"type": "event_msg", "payload": {"type": "token_count"}},
    ])
    entries = _parse_whole(path, st.codex_entries)
    assert _kinds(entries) == ["system", "system", "user", "thinking", "assistant",
                               "tool_call", "tool_call", "system"]
    assert [e["label"] for e in entries if e["kind"] == "system"] == ["developer", "system", "sub-agent"]
    assert entries[2]["text"] == "review #877"
    assert entries[5]["name"] == "exec" and entries[5]["result"] == "done"
    assert entries[6]["name"] == "shell" and entries[6]["result"] == "a\nb"
    assert entries[7]["sidechain"] is True


def test_codex_pages_concatenate(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(st, "WINDOW_BYTES", 250)
    lines = []
    for i in range(30):
        lines.append(_codex({"type": "message", "role": "user",
                             "content": [{"type": "input_text", "text": f"ask {i}"}]}))
        lines.append(_codex({"type": "custom_tool_call", "call_id": f"c{i}", "name": "exec", "input": f"run {i}"}))
        lines.append(_codex({"type": "custom_tool_call_output", "call_id": f"c{i}",
                             "output": [{"type": "input_text", "text": "ok" * (i % 9)}]}))
        lines.append(_codex({"type": "message", "role": "assistant",
                             "content": [{"type": "output_text", "text": f"answer {i}"}]}))
    path = _write_jsonl(tmp_path / "rollout.jsonl", lines)
    pages = _walk(path, 5, flavor="codex")
    assert len(pages) > 1
    assert [e for page in reversed(pages) for e in page] == _parse_whole(path, st.codex_entries)


# --------------------------------------------------------------- router


def test_transcript_path_classified_passkey():
    from app.webapp.middleware import _terminal_guard_level
    assert _terminal_guard_level("/api/claude-code/sessions/abc/transcript") == "passkey"


def test_transcript_refused_off_tailnet(webapp_client):
    client, _, _ = webapp_client
    assert client.get("/api/claude-code/sessions/s1/transcript").status_code == 403


@pytest.fixture
def _bypass_gate(monkeypatch):
    from app.webapp import middleware
    monkeypatch.setattr(
        middleware, "LOOPBACK_HOSTS",
        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
    )


def _live(sid: str = "s1", **extra: Any) -> Dict[str, Any]:
    row = {"session_id": sid, "kind": "pty", "agent": "claude", "alive": True,
           "project_dir": "E:/automation/proj", "started_at": 1_789_000_000}
    row.update(extra)
    return row


class TestTranscriptEndpoint:

    def test_unknown_session(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = []
        body = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert body["available"] is False and body["reason"] == "session_not_found"
        assert body["entries"] == [] and body["next_cursor"] is None

    def test_detached_claude_row_reads_its_claimed_history(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path
    ):
        # #966: a detached row has no PTY capture, but the reader never uses one.
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(kind="remote")]
        path = _write_jsonl(tmp_path / "t.jsonl", _conversation(2))
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: {"transcript_path": str(path)})
        body = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert body["available"] is True and body["source"] == "native"
        assert "prompt 1 p" in [e["text"] for e in body["entries"] if e["kind"] == "user"]

    def test_detached_codex_row_reads_its_rollout(self, webapp_client, _bypass_gate, monkeypatch, tmp_path):
        from app.webapp.routers import session_transcript as router_mod
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(kind="remote", agent="codex")]
        path = _write_jsonl(tmp_path / "rollout.jsonl", [
            _codex({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hey"}]}),
        ])
        monkeypatch.setattr(router_mod, "_find_codex_transcript", lambda session: path)
        body = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert body["available"] is True and body["source"] == "codex"
        monkeypatch.setattr(router_mod, "_find_codex_transcript", lambda session: None)
        assert client.get("/api/claude-code/sessions/s1/transcript").json()["reason"] == "no_transcript"

    def test_detached_unsupported_agent(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(kind="remote", agent="pi")]
        assert client.get("/api/claude-code/sessions/s1/transcript").json()["reason"] == "unsupported_agent"

    def test_unsupported_agent(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(agent="pi")]
        assert client.get("/api/claude-code/sessions/s1/transcript").json()["reason"] == "unsupported_agent"

    def test_no_transcript_when_no_row_or_no_file(self, webapp_client, _bypass_gate, monkeypatch, tmp_path):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live()]
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: None)
        assert client.get("/api/claude-code/sessions/s1/transcript").json()["reason"] == "no_transcript"
        monkeypatch.setattr(
            board, "state_row_for_session",
            lambda live, rows, sid: {"transcript_path": str(tmp_path / "gone.jsonl")},
        )
        assert client.get("/api/claude-code/sessions/s1/transcript").json()["reason"] == "no_transcript"

    def test_read_failure_is_distinct_from_missing(self, webapp_client, _bypass_gate, monkeypatch, tmp_path):
        from app.webapp.routers import session_transcript as router_mod
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live()]
        path = _write_jsonl(tmp_path / "t.jsonl", [_user("hi")])
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: {"transcript_path": str(path)})

        def boom(*args, **kwargs):
            raise PermissionError("locked")

        monkeypatch.setattr(router_mod, "transcript_page", boom)
        assert client.get("/api/claude-code/sessions/s1/transcript").json()["reason"] == "read_failed"

    def test_happy_path_pages_and_never_logs_bodies(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path, caplog
    ):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live()]
        path = _write_jsonl(tmp_path / "t.jsonl", _conversation(6, tool_calls_per_turn=1))
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: {"transcript_path": str(path)})

        with caplog.at_level(logging.INFO):
            first = client.get("/api/claude-code/sessions/s1/transcript?limit=4").json()
        assert first["available"] is True and first["source"] == "native"
        assert first["session_id"] == "s1" and first["reason"] is None
        assert [e["text"] for e in first["entries"] if e["kind"] == "user"] == ["prompt 4 pppp", "prompt 5 ppppp"]
        assert isinstance(first["next_cursor"], int)
        older = client.get(
            f"/api/claude-code/sessions/s1/transcript?limit=4&before={first['next_cursor']}"
        ).json()
        assert [e["text"] for e in older["entries"] if e["kind"] == "user"] == ["prompt 2 pp", "prompt 3 ppp"]
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "prompt 5" not in joined and "reply 5" not in joined
        assert "transcript s1" in joined  # the breadcrumb itself is there

    def test_codex_source(self, webapp_client, _bypass_gate, monkeypatch, tmp_path):
        from app.webapp.routers import session_transcript as router_mod
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(agent="codex")]
        path = _write_jsonl(tmp_path / "rollout.jsonl", [
            _codex({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hey"}]}),
            _codex({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "yo"}]}),
        ])
        monkeypatch.setattr(router_mod, "_find_codex_transcript", lambda session: path)
        body = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert body["source"] == "codex" and _kinds(body["entries"]) == ["user", "assistant"]

    def test_limit_is_validated(self, webapp_client, _bypass_gate, monkeypatch):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live()]
        assert client.get("/api/claude-code/sessions/s1/transcript?limit=0").status_code == 422
        assert client.get("/api/claude-code/sessions/s1/transcript?limit=101").status_code == 422
        assert client.get("/api/claude-code/sessions/s1/transcript?before=-1").status_code == 422
