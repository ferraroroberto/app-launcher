"""Coding-tab transcript: tool actions, image refs and the running-step activity label."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src import board
from src.session_view import transcript as st
from src.transcript_flavors import _shared as tf_shared

from tests.unit.transcript._support import (
    _assistant,
    _codex,
    _live,
    _parse_whole,
    _png_b64,
    _tool_result,
    _tool_use,
    _user,
    _write_jsonl,
)


# ------------------------------------------------ plain-words actions (#1266)


def test_tool_action_covers_the_probed_tools_of_every_harness():
    """The command, edit, write and read tools probed from each harness's
    real transcripts (key names only) read in plain words; +N -M is a line
    diff of the edit's own old and new text."""
    act = tf_shared._tool_action
    # Commands keep their line breaks: Chat shows the first line folded and
    # the whole command in the opened block.
    assert act("Bash", {"command": "cd x\npytest -q"}) == {"verb": "ran", "command": "cd x\npytest -q"}
    assert act("run_terminal_command", {"command": "ls"}) == {"verb": "ran", "command": "ls"}
    assert act("run_command", {"CommandLine": "dir", "Cwd": "C:/"}) == {"verb": "ran", "command": "dir"}
    assert act("exec", "git status", raw_text=True) == {"verb": "ran", "command": "git status"}
    # A JSON argument string is never mistaken for a raw command.
    assert act("exec", '{"cmd": "ls"}') is None
    # Edits and writes also carry their diff (#1349): tests/test_transcript_diffs.py.
    def plain(action):
        return {k: v for k, v in action.items() if k != "diff"}

    assert plain(act("Edit", {"file_path": "a/b.py", "old_string": "x\ny", "new_string": "x\nz\nw"})) == {
        "verb": "edited", "path": "a/b.py", "added": 2, "removed": 1}
    assert plain(act("edit", {"path": "c.md", "edits": [
        {"oldText": "a", "newText": "a\nb"}, {"oldText": "q\nr", "newText": ""}]})) == {
        "verb": "edited", "path": "c.md", "added": 1, "removed": 2}
    assert plain(act("Write", {"file_path": "n.txt", "content": "1\n2\n3"})) == {
        "verb": "wrote", "path": "n.txt", "added": 3, "removed": 0}
    assert act("write_to_file", {"TargetFile": "m.txt", "CodeContent": "x"})["added"] == 1
    for name, key in (("Read", "file_path"), ("read", "path"), ("read_file", "target_file"),
                      ("view_file", "AbsolutePath"), ("view", "path")):
        assert act(name, {key: "f.py"}) == {"verb": "read", "path": "f.py"}
    # Unknown tools and inputs missing their key keep today's rendering.
    assert act("Grep", {"pattern": "x"}) is None
    assert act("Edit", {"file_path": "a.py"}) is None
    assert act("Bash", {"command": "   "}) is None


def test_claude_images_are_refs_numbered_across_the_line(tmp_path: Path):
    """A pasted image and a screenshot read back through a tool result both
    become refs (#1265); a block with no data keeps ``[image]``. ``n`` walks
    the line, a ``tool_result``'s own content in place, so two results on one
    line never share a number. The route refuses anything that isn't an
    allowlisted image at exactly that spot."""
    png = _png_b64()

    def img(data):
        return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}

    path = _write_jsonl(tmp_path / "c.jsonl", [
        _user([{"type": "text", "text": "Look"}, img(png), {"type": "image", "source": {}}]),
        _assistant([_tool_use("Read", {"file_path": "a.png"}, "t1"),
                    _tool_use("Read", {"file_path": "b.png"}, "t2")], "m1"),
        _user([_tool_result("t1", [img(png)]), _tool_result("t2", [{"type": "text", "text": "b"}, img(png)])]),
    ])
    entries = _parse_whole(path)
    prompt = entries[0]
    assert prompt["text"] == "Look\n\n[image]"          # the data-less block keeps its placeholder
    assert prompt["images"] == [{"offset": 0, "n": 0}]
    first, second = [e for e in entries if e["kind"] == "tool_call"]
    assert [r["n"] for r in first["result_images"]] == [0]
    assert [r["n"] for r in second["result_images"]] == [1]
    assert first["result_images"][0]["offset"] == second["result_images"][0]["offset"] > 0

    ref = second["result_images"][0]
    assert st.transcript_image(path, ref["offset"], 1, "claude")[1] == "image/png"
    assert st.transcript_image(path, ref["offset"], 2, "claude") is None      # no such block
    assert st.transcript_image(path, ref["offset"] + 1, 0, "claude") is None  # not a line start
    assert st.transcript_image(path, 0, 1, "claude") is None                  # data-less block
    assert st.transcript_image(path, 0, 0, "codex") is None                   # records no images

    # Bytes that aren't an allowlisted image are refused, whatever the block says.
    bad = _write_jsonl(tmp_path / "bad.jsonl", [_user([img("PHN2Zz48L3N2Zz4=")])])   # "<svg></svg>"
    assert st.transcript_image(bad, 0, 0, "claude") is None


# ------------------------------------------------ live activity line (#1387)


def _claude_turn(*rows: Dict[str, Any]) -> List[Dict[str, Any]]:
    """An earlier finished exchange with two tool calls, then ``rows``: the
    count must restart at the last user message."""
    return [
        _user("earlier"),
        _assistant([_tool_use("Bash", {"command": "ls"}, "e1"),
                    _tool_use("Read", {"file_path": "a"}, "e2")], "m0"),
        _user([_tool_result("e1", "x"), _tool_result("e2", "y")]),
        _assistant([{"type": "text", "text": "done"}], "m0b"),
        _user("now do the next thing", timestamp="2026-09-14T11:00:00Z"),
        *rows,
    ]


@pytest.mark.parametrize("call, label", [
    (_tool_use("Bash", {"command": "pytest"}, "t1"), "Running Bash"),
    (_tool_use("Edit", {"file_path": "a", "old_string": "x", "new_string": "y"}, "t1"), "Editing a file"),
    (_tool_use("Write", {"file_path": "a", "content": "x"}, "t1"), "Editing a file"),
    (_tool_use("Read", {"file_path": "a"}, "t1"), "Reading a file"),
    (_tool_use("WebFetch", {"url": "https://example.com"}, "t1"), "WebFetch"),
])
def test_activity_labels_the_running_step(tmp_path: Path, call, label):
    path = _write_jsonl(tmp_path / "c.jsonl", _claude_turn(_assistant([call], "m1")))
    got = st.transcript_activity(path, now=path.stat().st_mtime)
    assert got == {"since": "2026-09-14T11:00:00Z", "actions": 1, "last": label, "working": True}


def test_activity_counts_only_the_turn_in_progress(tmp_path: Path):
    path = _write_jsonl(tmp_path / "c.jsonl", _claude_turn(
        _assistant([_tool_use("Bash", {"command": "a"}, "t1"),
                    _tool_use("Bash", {"command": "b"}, "t2")], "m1"),
        _user([_tool_result("t1", "ok")]),
        _assistant([{"type": "thinking", "thinking": "hmm"}], "m2"),
    ))
    got = st.transcript_activity(path, now=path.stat().st_mtime)
    assert (got["actions"], got["last"], got["working"]) == (2, "Thinking", True)


def test_activity_is_thinking_with_no_actions_right_after_a_prompt(tmp_path: Path):
    path = _write_jsonl(tmp_path / "c.jsonl", _claude_turn())
    got = st.transcript_activity(path, now=path.stat().st_mtime)
    assert (got["actions"], got["last"], got["working"]) == (0, "Thinking", True)


def test_activity_for_a_codex_rollout(tmp_path: Path):
    path = _write_jsonl(tmp_path / "rollout.jsonl", [
        _codex({"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "review #877"}]}),
        _codex({"type": "function_call", "name": "shell",
                "arguments": '{"command": "git diff"}', "call_id": "c1"}),
        _codex({"type": "function_call_output", "call_id": "c1", "output": "ok"}),
        _codex({"type": "function_call", "name": "shell",
                "arguments": '{"command": "pytest"}', "call_id": "c2"}),
    ])
    got = st.transcript_activity(path, flavor="codex", now=path.stat().st_mtime)
    assert got == {"since": "2026-09-08T15:49:10Z", "actions": 2, "last": "shell", "working": True}


def test_activity_without_tool_data_carries_the_time_alone(tmp_path: Path, monkeypatch):
    """A harness that records no tool calls reports `actions: None` — "0
    actions" would be a guess."""
    path = _write_jsonl(tmp_path / "c.jsonl", _claude_turn(
        _assistant([_tool_use("Bash", {"command": "a"}, "t1")], "m1")))
    entries = _parse_whole(path)
    assert st.turn_activity(entries, tool_calls=False)["actions"] is None
    monkeypatch.setattr(st, "_NO_TOOL_CALL_FLAVORS", frozenset({"claude"}))
    got = st.transcript_activity(path, now=path.stat().st_mtime)
    assert got["actions"] is None and got["since"] == "2026-09-14T11:00:00Z"


def test_activity_working_needs_an_open_fresh_turn_or_the_busy_signal(tmp_path: Path):
    open_turn = _write_jsonl(tmp_path / "open.jsonl", _claude_turn(
        _assistant([_tool_use("Bash", {"command": "a"}, "t1")], "m1")))
    mtime = open_turn.stat().st_mtime
    stale = mtime + st.ACTIVITY_FRESH_S + 1
    assert st.transcript_activity(open_turn, now=stale)["working"] is False   # interrupted: never written again
    assert st.transcript_activity(open_turn, now=stale, busy=True)["working"] is True

    finished = _write_jsonl(tmp_path / "done.jsonl", _claude_turn(
        _assistant([{"type": "text", "text": "All done."}], "m1")))
    assert st.transcript_activity(finished, now=finished.stat().st_mtime)["working"] is False
    assert st.transcript_activity(finished, now=finished.stat().st_mtime, busy=True)["working"] is True


def test_activity_is_none_when_no_user_message_is_in_reach(tmp_path: Path):
    path = _write_jsonl(tmp_path / "c.jsonl", [
        _assistant([{"type": "text", "text": "hello"}], "m1")])
    assert st.transcript_activity(path) is None
    assert st.transcript_activity(tmp_path / "missing.jsonl") is None


def test_activity_reads_the_file_only_when_it_changed(tmp_path: Path, monkeypatch):
    """An idle chat view ticks every 3 s: an unchanged file must cost a stat,
    not a page read (#1050's cost model)."""
    path = _write_jsonl(tmp_path / "c.jsonl", _claude_turn(
        _assistant([_tool_use("Bash", {"command": "a"}, "t1")], "m1")))
    reads = []
    real = st._page
    monkeypatch.setattr(st, "_page", lambda *a, **k: (reads.append(1), real(*a, **k))[1])
    st.transcript_activity(path)
    st.transcript_activity(path)
    assert len(reads) == 1
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_user([_tool_result("t1", "ok")])) + "\n")
    assert st.transcript_activity(path)["last"] == "Thinking"
    assert len(reads) == 2


class TestTranscriptActivityEndpoint:
    """#1387: the chat strip's activity line rides the existing page and tail
    responses — no route or poll of its own."""

    def _serve(self, webapp_client, monkeypatch, path: Path, **live: Any):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(**live)]
        monkeypatch.setattr(
            board, "state_row_for_session",
            lambda live_rows, rows, sid: {"transcript_path": str(path)},
        )
        return client

    def test_the_newest_page_and_every_tail_tick_carry_it(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path
    ):
        path = _write_jsonl(tmp_path / "t.jsonl", _claude_turn(
            _assistant([_tool_use("Bash", {"command": "pytest"}, "t1")], "m1")))
        client = self._serve(webapp_client, monkeypatch, path)
        url = "/api/claude-code/sessions/s1/transcript"

        page = client.get(url).json()
        assert page["activity"]["last"] == "Running Bash" and page["activity"]["actions"] == 1
        assert page["activity"]["since"] == "2026-09-14T11:00:00Z"
        # An older page is not the chat's first read and carries none.
        older = client.get(f"{url}?before={path.stat().st_size}").json()
        assert older["available"] is True and "activity" not in older

        unchanged = client.get(f"{url}?after={page['tail']}&size={page['size']}").json()
        assert unchanged["changed"] is False
        assert unchanged["activity"]["last"] == "Running Bash"

    def test_claudes_busy_title_marks_a_finished_looking_turn_as_working(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path
    ):
        path = _write_jsonl(tmp_path / "t.jsonl", _claude_turn(
            _assistant([{"type": "text", "text": "Still going."}], "m1")))
        os.utime(path, (1_000_000.0, 1_000_000.0))   # long stale: only the title can say working
        url = "/api/claude-code/sessions/s1/transcript"
        idle = self._serve(webapp_client, monkeypatch, path, live_title="✳ Fix it")
        assert idle.get(url).json()["activity"]["working"] is False
        busy = self._serve(webapp_client, monkeypatch, path, live_title="⠋ Fix it")
        assert busy.get(url).json()["activity"]["working"] is True
        # A wedged PTY (no output for ages) leaves the glyph frozen: not working.
        wedged = self._serve(webapp_client, monkeypatch, path, live_title="⠋ Fix it", last_output_at=1.0)
        assert wedged.get(url).json()["activity"]["working"] is False
