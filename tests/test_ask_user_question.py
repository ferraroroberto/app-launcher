"""Chat's AskUserQuestion card (#1149): reading the call, gating an answer,
and turning a pick into the picker's keystrokes.

The keystroke sequences pinned here are the ones probed against a live
Claude Code 2.1.281 picker, in both a PTY and a detached console (evidence on
the issue): a digit jump-selects (and, for a single-select question, moves
on), Tab leaves a multi-select question, option ``n + 1`` opens the typed
answer, and a multi-part call ends on a review tab whose ``1`` submits.
Every transcript here is synthetic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src import board_transcript, session_transcript as st
from src.ask_user_question import answer_keystrokes, answers_from_result, questions_from_input


def _q(question: str, labels: List[str], multi: bool = False) -> Dict[str, Any]:
    return {
        "question": question, "header": question.split()[0], "multiSelect": multi,
        "options": [{"label": lab, "description": f"about {lab}"} for lab in labels],
    }


COLOUR = _q("Pick a colour", ["Red", "Green", "Blue"])
SIZE = _q("Pick a size", ["Small", "Large"])
FRUIT = _q("Pick fruits", ["Apple", "Banana", "Cherry"], multi=True)


# ------------------------------------------------------------- keystrokes


@pytest.mark.parametrize("questions,answers,keys", [
    # One single-select question: the digit alone answers it. An Enter after
    # it would land in the composer once the picker has closed.
    ([COLOUR], [{"option": 2}], [("2", False)]),
    # "Type something" is option n + 1, then the text, then Enter.
    ([COLOUR], [{"text": "Purple haze"}], [("4", False), ("Purple haze", True)]),
    # Two questions: a digit each, then the review tab's "Submit answers".
    ([COLOUR, SIZE], [{"option": 3}, {"option": 2}], [("3", False), ("2", False), ("1", False)]),
    # Multi-select: toggles, Tab to move on, then submit — in pick order
    # regardless of the order they were tapped.
    ([FRUIT], [{"options": [3, 1]}], [("1", False), ("3", False), ("\t", False), ("1", False)]),
    # The mixed call probed end to end through the route in both kinds.
    ([FRUIT, COLOUR], [{"options": [2, 3]}, {"text": "Teal blue"}],
     [("2", False), ("3", False), ("\t", False), ("4", False), ("Teal blue", True), ("1", False)]),
])
def test_keystrokes_match_the_probed_picker(questions, answers, keys):
    assert answer_keystrokes(questions, answers) == keys


def test_typed_answer_is_one_line():
    """A newline would be an Enter mid-answer; whitespace runs collapse."""
    assert answer_keystrokes([COLOUR], [{"text": "  two\nlines  "}]) == [("4", False), ("two lines", True)]


@pytest.mark.parametrize("questions,answers,why", [
    ([COLOUR], [], "expected 1 answer"),
    ([COLOUR], [{"option": 4}], "out of range"),        # 4 is "Type something", not an option
    ([COLOUR], [{"option": 0}], "out of range"),
    ([COLOUR], [{"option": True}], "out of range"),     # a bool is not an index
    ([COLOUR], [{"option": "2"}], "out of range"),
    ([COLOUR], [{"text": "   "}], "empty"),
    ([COLOUR], [{"text": "x" * 501}], "over 500"),
    ([FRUIT], [{"options": []}], "at least one"),
    ([FRUIT], [{"options": [1, 1]}], "out of range"),
    ([FRUIT], [{"text": "Mango"}], "at least one"),     # typed multi-select was never probed
    ([COLOUR], ["2"], "not an object"),
    ([], [], "no options"),
    ([_q("Too many", [str(i) for i in range(9)])], [{"option": 1}], "too many options"),
])
def test_a_malformed_answer_builds_no_keys(questions, answers, why):
    with pytest.raises(ValueError, match=why):
        answer_keystrokes(questions, answers)


# ---------------------------------------------------------------- reading


def test_questions_are_sanitized_and_an_unknown_shape_is_refused():
    raw = {"questions": [dict(COLOUR, question="  Pick a colour  ", extra="dropped")]}
    out = questions_from_input(raw)
    assert out == [{
        "question": "Pick a colour", "header": "Pick", "multiSelect": False,
        "options": [{"label": lab, "description": f"about {lab}"} for lab in ("Red", "Green", "Blue")],
    }]
    # Anything this reader doesn't know keeps the generic row instead.
    for bad in ({}, {"questions": "x"}, {"questions": [{"options": []}]},
                {"questions": [{"options": [{"description": "no label"}]}]}):
        assert questions_from_input(bad) is None, bad


def test_answers_come_from_the_line_and_a_declined_call_has_none():
    assert answers_from_result({"answers": {"Pick a colour": "Green"}, "questions": []}) == {"Pick a colour": "Green"}
    assert answers_from_result("User rejected tool use") is None
    assert answers_from_result({"answers": {}}) is None


def _jsonl(path: Path, rows: List[Dict[str, Any]]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def _ask(tid: str, questions: List[Dict[str, Any]], mid: str = "m1", **extra: Any) -> Dict[str, Any]:
    row = {"type": "assistant", "timestamp": "2026-09-24T02:00:00Z",
           "message": {"id": mid, "role": "assistant", "content": [
               {"type": "tool_use", "id": tid, "name": "AskUserQuestion",
                "input": {"questions": questions}}]}}
    row.update(extra)
    return row


def _answer(tid: str, answers: Any, error: bool = False) -> Dict[str, Any]:
    block = {"type": "tool_result", "tool_use_id": tid, "content": "answered"}
    if error:
        block["is_error"] = True
    return {"type": "user", "timestamp": "2026-09-24T02:00:05Z", "toolUseResult": answers,
            "message": {"role": "user", "content": [block]}}


def _lines(path: Path):
    raw, pos, out = path.read_bytes(), 0, []
    for chunk in raw.split(b"\n"):
        if chunk.strip():
            out.append((pos, chunk.decode("utf-8")))
        pos += len(chunk) + 1
    return out


def test_claude_entries_carry_the_question_and_its_answer(tmp_path: Path):
    path = _jsonl(tmp_path / "t.jsonl", [
        _ask("t1", [COLOUR]),
        _answer("t1", {"questions": [COLOUR], "answers": {"Pick a colour": "Green"}}),
        {"type": "assistant", "timestamp": "2026-09-24T02:00:06Z", "message": {
            "id": "m2", "role": "assistant", "content": [
                {"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "a.md"}}]}},
        _answer("t2", "file body"),
    ])
    ask, read = st.claude_entries(_lines(path))
    assert ask["name"] == "AskUserQuestion" and ask["call_id"] == "t1"
    assert [o["label"] for o in ask["questions"][0]["options"]] == ["Red", "Green", "Blue"]
    assert ask["answers"] == {"Pick a colour": "Green"}
    # The generic path is untouched for every other tool.
    assert "questions" not in read and "call_id" not in read and "answers" not in read
    assert read["summary"] == "a.md"


def test_an_answer_whose_call_is_on_an_earlier_page_still_carries_it(tmp_path: Path):
    """The live tick can settle the call before its result arrives; the
    standalone result keeps the picks so the card can still close."""
    path = _jsonl(tmp_path / "t.jsonl", [_answer("t1", {"answers": {"Pick a colour": "Blue"}})])
    (entry,) = st.claude_entries(_lines(path))
    assert entry["kind"] == "tool_result" and entry["tool_use_id"] == "t1"
    assert entry["answers"] == {"Pick a colour": "Blue"}


def test_a_declined_question_is_marked_failed_with_no_answers(tmp_path: Path):
    path = _jsonl(tmp_path / "t.jsonl", [
        _ask("t1", [COLOUR]), _answer("t1", "User rejected tool use", error=True),
    ])
    (ask,) = st.claude_entries(_lines(path))
    assert ask["error"] is True and "answers" not in ask


# ------------------------------------------------------ pending (send gate)


def test_pending_call_is_the_newest_unanswered_question(tmp_path: Path):
    path = _jsonl(tmp_path / "t.jsonl", [
        _ask("old", [COLOUR], mid="m1"),
        _answer("old", {"answers": {"Pick a colour": "Red"}}),
        _ask("live", [SIZE], mid="m2"),
        # Newer, but a sub-agent's: never the question the user is asked.
        _ask("sub", [COLOUR], mid="m3", isSidechain=True),
    ])
    pending = board_transcript.pending_decision_call(path)
    assert pending["id"] == "live" and pending["name"] == "AskUserQuestion"
    assert pending["input"]["questions"][0]["question"] == "Pick a size"


def test_nothing_pending_once_answered_or_unreadable(tmp_path: Path):
    path = _jsonl(tmp_path / "t.jsonl", [_ask("t1", [COLOUR]), _answer("t1", {"answers": {}})])
    assert board_transcript.pending_decision_call(path) is None
    assert board_transcript.pending_decision_call(tmp_path / "missing.jsonl") is None
    assert board_transcript.pending_decision_call(None) is None


# ------------------------------------------------------------------ route


def _live(kind: str = "pty", agent: str = "claude") -> Dict[str, Any]:
    return {"session_id": "s1", "kind": kind, "agent": agent, "alive": True,
            "project_dir": "E:/automation/proj", "started_at": 1_789_000_000}


@pytest.fixture
def answer_client(webapp_client, monkeypatch, tmp_path):
    """The webapp with the gate bypassed, one live Claude session whose
    transcript is ``path``, no key gap, and the PTY socket replaced by a
    recorder — nothing here opens a real socket."""
    from app.webapp import middleware
    from app.webapp.routers import session_transcript as router

    monkeypatch.setattr(middleware, "LOOPBACK_HOSTS",
                        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}))
    client, _, overrides = webapp_client
    session = overrides["session"]
    session.list_sessions.return_value = [_live()]
    path = tmp_path / "t.jsonl"
    from src import board
    monkeypatch.setattr(board, "state_row_for_session",
                        lambda live, rows, sid: {"transcript_path": str(path)})
    monkeypatch.setattr(router, "_ANSWER_KEY_GAP_S", 0)
    typed: List[Any] = []

    async def fake_pty(port, sid, keys):
        typed.append((sid, keys))

    monkeypatch.setattr(router, "_type_into_pty", fake_pty)
    return client, session, path, typed


def _post(client, body: Dict[str, Any]):
    return client.post("/api/claude-code/sessions/s1/answer", json=body)


def test_answer_route_is_passkey_gated():
    from app.webapp.middleware import _terminal_guard_level
    assert _terminal_guard_level("/api/claude-code/sessions/abc/answer") == "passkey"


def test_answer_types_the_keys_built_from_the_transcripts_copy(answer_client):
    client, _, path, typed = answer_client
    _jsonl(path, [_ask("t1", [COLOUR, SIZE])])
    r = _post(client, {"tool_use_id": "t1", "answers": [{"option": 1}, {"option": 2}]})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "delivered": "unconfirmed", "steps": 3, "kind": "pty"}
    assert typed == [("s1", [("1", False), ("2", False), ("1", False)])]


def test_a_question_no_longer_waiting_types_nothing(answer_client):
    client, _, path, typed = answer_client
    _jsonl(path, [
        _ask("t1", [COLOUR], mid="m1"), _answer("t1", {"answers": {"Pick a colour": "Red"}}),
        _ask("t2", [SIZE], mid="m2"),
    ])
    for call_id in ("t1", "nope"):       # answered already / never existed
        r = _post(client, {"tool_use_id": call_id, "answers": [{"option": 1}]})
        assert r.status_code == 409 and "no longer waiting" in r.json()["detail"]
    assert typed == []


def test_an_ended_session_and_a_bad_answer_are_refused(answer_client):
    client, session, path, typed = answer_client
    _jsonl(path, [_ask("t1", [COLOUR])])
    assert _post(client, {"answers": [{"option": 1}]}).status_code == 400
    r = _post(client, {"tool_use_id": "t1", "answers": [{"option": 9}]})
    assert r.status_code == 400 and "out of range" in r.json()["detail"]
    session.list_sessions.return_value = []
    r = _post(client, {"tool_use_id": "t1", "answers": [{"option": 1}]})
    assert r.status_code == 409 and "no longer running" in r.json()["detail"]
    session.list_sessions.return_value = [_live(agent="codex")]
    assert _post(client, {"tool_use_id": "t1", "answers": [{"option": 1}]}).status_code == 409
    assert typed == []


def test_detached_answer_is_one_console_send_per_step(answer_client):
    client, session, path, typed = answer_client
    session.list_sessions.return_value = [_live(kind="remote")]
    session.send_input.return_value = {"ok": True}
    _jsonl(path, [_ask("t1", [COLOUR])])
    r = _post(client, {"tool_use_id": "t1", "answers": [{"text": "Teal"}]})
    assert r.status_code == 200 and r.json()["kind"] == "remote"
    assert [c.args[2:] for c in session.send_input.call_args_list] == [("4", False), ("Teal", True)]
    assert typed == []


def test_a_send_that_fails_part_way_says_how_far_it_got(answer_client):
    client, session, path, _ = answer_client
    session.list_sessions.return_value = [_live(kind="remote")]
    session.send_input.side_effect = [{"ok": True}, session.SessionHostError("console gone")]
    _jsonl(path, [_ask("t1", [COLOUR, SIZE])])
    r = _post(client, {"tool_use_id": "t1", "answers": [{"option": 1}, {"option": 1}]})
    assert r.status_code == 502
    assert r.json()["detail"] == "Answer only partly sent (1 of 3 keys): check the PC console"
