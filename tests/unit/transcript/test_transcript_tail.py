"""Coding-tab transcript: the live tail replay."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from src import board, session_transcript as st

from tests.unit.transcript._support import (
    _assistant,
    _conversation,
    _grok,
    _grok_chunk,
    _kinds,
    _live,
    _tool_result,
    _tool_use,
    _user,
    _write_jsonl,
)


# ------------------------------------------------- transcript_tail (#1050)
#
# The forward cursor a live chat view ticks on. Two failure modes are what
# these exist for, and both are invisible in a single-shot read: a message
# split across a tick boundary rendering as two cards, and a turn dropped at
# a seam. `test_live_replay_matches_one_whole_read` is the one that pins
# them, by replaying a conversation a few lines at a time and demanding the
# result be identical to reading the finished file once.


def _append_jsonl(path: Path, lines: List[Dict[str, Any]]) -> None:
    with path.open("a", encoding="utf-8", newline="") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")


def _turn_texts(entries: List[Dict[str, Any]]) -> List[Tuple[str, str]]:
    return [(e["kind"], e.get("text", "")) for e in entries if st._is_turn(e)]


def _live_view(path: Path, flavor: str = "claude"):
    """Open a chat view: the newest page plus its live cursor."""
    page = st.transcript_page(path, limit=100, flavor=flavor)
    return list(page["entries"]), page["tail"], page["size"]


def test_tail_of_an_unchanged_file_reads_nothing(tmp_path: Path, monkeypatch):
    """The pre-check that makes an idle chat view nearly free: same size, no
    open, no parse — the claim the whole polling design rests on."""
    path = _write_jsonl(tmp_path / "t.jsonl", _conversation(3))
    _, tail, size = _live_view(path)
    opened: List[str] = []
    real_open = Path.open

    def spy(self, *a, **k):
        opened.append(str(self))
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", spy)
    out = st.transcript_tail(path, after=tail, size=size)
    assert out["changed"] is False and out["reset"] is False
    assert out["entries"] == [] and out["pending"] == []
    assert opened == [], "an unchanged file must not be opened at all"


def test_tail_returns_only_what_was_appended(tmp_path: Path):
    path = _write_jsonl(tmp_path / "t.jsonl", _conversation(2))
    _, tail, size = _live_view(path)
    _append_jsonl(path, [_user("brand new prompt")])
    out = st.transcript_tail(path, after=tail, size=size)
    assert out["changed"] is True and out["reset"] is False
    texts = [e.get("text") for e in out["entries"] + out["pending"]]
    assert "brand new prompt" in texts
    assert "prompt 0 " not in texts, "nothing already seen is resent"


def test_tail_holds_back_a_message_that_is_still_growing(tmp_path: Path):
    """A Claude message is one line per content block under one id. Settling
    the first line would render the reply, then render it *again* as a second
    card when its trailing text arrives — so the whole span stays pending
    until a differently-keyed line follows it."""
    path = _write_jsonl(tmp_path / "t.jsonl", [_user("go")])
    _, tail, size = _live_view(path)
    _append_jsonl(path, [_assistant([{"type": "text", "text": "first half"}], "m9")])
    out = st.transcript_tail(path, after=tail, size=size)
    # The prompt settles (it is complete and something now follows it); the
    # message that just started does not.
    assert [e["kind"] for e in out["entries"]] == ["user"]
    assert [e["kind"] for e in out["pending"]] == ["assistant"]

    # The rest of the same message lands; it is still one card, not two.
    _append_jsonl(path, [
        _assistant([_tool_use("Bash", {"command": "ls"}, "t9")], "m9"),
        _assistant([{"type": "text", "text": " and second half"}], "m9"),
    ])
    out = st.transcript_tail(path, after=out["tail"], size=out["size"])
    replies = [e for e in out["entries"] + out["pending"] if e["kind"] == "assistant"]
    assert len(replies) == 1, "one message must never become two cards"
    # Both blocks are in that one card (joined the way the builder joins
    # blocks), so nothing was dropped to achieve the single card either.
    assert "first half" in replies[0]["text"]
    assert "and second half" in replies[0]["text"]


def test_tail_settles_a_message_once_the_next_one_starts(tmp_path: Path):
    path = _write_jsonl(tmp_path / "t.jsonl", [_user("go")])
    _, tail, size = _live_view(path)
    _append_jsonl(path, [_assistant([{"type": "text", "text": "done"}], "m1")])
    out = st.transcript_tail(path, after=tail, size=size)
    assert [e["kind"] for e in out["pending"]] == ["assistant"]
    _append_jsonl(path, [_assistant([{"type": "text", "text": "next"}], "m2")])
    out = st.transcript_tail(path, after=out["tail"], size=out["size"])
    # The finished message is now settled — appended once and never touched
    # again — and only the new one is provisional.
    assert [e["text"] for e in out["entries"]] == ["done"]
    assert [e["text"] for e in out["pending"]] == ["next"]


def test_tail_keys_every_entry_so_a_rebuild_can_restore_open_cards(tmp_path: Path):
    """The live region is re-rendered whenever it changes, so each card needs
    a stable identity — including the folded kinds, which a page strips."""
    path = _write_jsonl(tmp_path / "t.jsonl", [_user("go")])
    _, tail, size = _live_view(path)
    _append_jsonl(path, [
        _assistant([{"type": "thinking", "thinking": "hmm"}], "m1"),
        _assistant([_tool_use("Bash", {"command": "ls"}, "t1")], "m1"),
        _user([_tool_result("t1", "out")]),
        _assistant([{"type": "text", "text": "done"}], "m2"),
    ])
    out = st.transcript_tail(path, after=tail, size=size)
    assert out["entries"], "the finished message should have settled"
    for e in out["entries"] + out["pending"]:
        assert isinstance(e.get("offset"), int), f"{e['kind']} carries no render key"


def test_tail_resets_when_the_file_is_rewritten_under_the_cursor(tmp_path: Path):
    path = _write_jsonl(tmp_path / "t.jsonl", _conversation(4))
    _, tail, size = _live_view(path)
    _write_jsonl(path, [_user("a whole new conversation")])  # rotated/compacted
    out = st.transcript_tail(path, after=tail, size=size)
    assert out["reset"] is True, "a cursor past EOF must ask for a reload"
    assert out["entries"] == [] and out["pending"] == []


def test_tail_resets_rather_than_replaying_a_huge_backlog(tmp_path: Path, monkeypatch):
    """A phone that was locked for an hour wants the newest turns, not a
    multi-MB replay of everything it missed."""
    monkeypatch.setattr(st, "REQUEST_BYTE_CAP", 2_000)
    path = _write_jsonl(tmp_path / "t.jsonl", [_user("go")])
    _, tail, size = _live_view(path)
    _append_jsonl(path, _conversation(30))
    out = st.transcript_tail(path, after=tail, size=size)
    assert out["reset"] is True


def test_tail_of_a_half_written_line_waits_for_the_rest(tmp_path: Path):
    """A half-written line is not a turn. It must not parse as one, and the
    cursor must not step over it — the next tick reads it whole."""
    path = _write_jsonl(tmp_path / "t.jsonl", [_user("go")])
    _, tail, size = _live_view(path)
    head = '{"type": "assistant", "message": {"id": "m1", "role'
    rest = '": "assistant", "content": [{"type": "text", "text": "hi"}]}}\n'
    with path.open("a", encoding="utf-8", newline="") as fh:
        fh.write(head)
    out = st.transcript_tail(path, after=tail, size=size)
    # Nothing of the torn line is shown, and the cursor has not stepped over
    # it: the offset it hands back still points at where that line begins.
    assert [e for e in out["entries"] + out["pending"] if e["kind"] == "assistant"] == []
    assert out["tail"] <= len(path.read_bytes()) - len(head)
    with path.open("a", encoding="utf-8", newline="") as fh:
        fh.write(rest)
    out = st.transcript_tail(path, after=out["tail"], size=None)
    assert [e["text"] for e in out["pending"] if e["kind"] == "assistant"] == ["hi"]


def test_tail_pairs_a_call_with_a_result_a_tick_later_for_an_unkeyed_flavour(tmp_path: Path):
    """#1310. Grok's line key is always ``None`` (self-contained lines), so
    a tool call and its result — always two separate lines — routinely land
    a tick apart. Before the fix, the "last line" fallback settled the call
    the moment it was read; the result then arrived with no open call in
    that tick's build and rendered as a standalone orphan card."""
    path = _write_jsonl(tmp_path / "updates.jsonl", [_grok_chunk("user_message_chunk", "go")])
    _, tail, size = _live_view(path, flavor="grok")

    _append_jsonl(path, [_grok({
        "sessionUpdate": "tool_call", "toolCallId": "t1",
        "title": "ls", "rawInput": {"command": "ls"},
    })])
    out = st.transcript_tail(path, after=tail, size=size, flavor="grok")
    # The prompt settles now that the call follows it; the call itself, with
    # no result yet, stays pending rather than settling with none.
    assert [e["kind"] for e in out["entries"]] == ["user"]
    assert [e["kind"] for e in out["pending"]] == ["tool_call"]

    _append_jsonl(path, [_grok({
        "sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed",
        "content": [{"type": "content", "content": {"type": "text", "text": "out"}}],
    })])
    out = st.transcript_tail(path, after=out["tail"], size=out["size"], flavor="grok")
    tool_kinds = [e["kind"] for e in out["entries"] + out["pending"] if e["kind"] != "user"]
    assert tool_kinds == ["tool_call"], (
        "the result must land inside its call's card, never as its own tool_result card"
    )
    calls = [e for e in out["entries"] + out["pending"] if e["kind"] == "tool_call"]
    assert len(calls) == 1 and calls[0]["result"] == "out"


def test_tail_pairs_a_call_and_result_that_land_in_the_same_read_unkeyed_flavour(tmp_path: Path):
    """#1310's other half: a call and its result that arrive *together* (a
    fresh page load, or a tail read that caught up in one tick) must not be
    split by a naive "just the last line" cut — that settles the call alone
    (no result) and leaves the result to render as an orphan."""
    path = _write_jsonl(tmp_path / "updates.jsonl", [_grok_chunk("user_message_chunk", "go")])
    _, tail, size = _live_view(path, flavor="grok")

    _append_jsonl(path, [
        _grok({"sessionUpdate": "tool_call", "toolCallId": "t1",
               "title": "ls", "rawInput": {"command": "ls"}}),
        _grok({"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed",
               "content": [{"type": "content", "content": {"type": "text", "text": "out"}}]}),
    ])
    out = st.transcript_tail(path, after=tail, size=size, flavor="grok")
    tool_kinds = [e["kind"] for e in out["entries"] + out["pending"] if e["kind"] != "user"]
    assert tool_kinds == ["tool_call"], (
        "the result must land inside its call's card, never as its own tool_result card"
    )
    calls = [e for e in out["entries"] + out["pending"] if e["kind"] == "tool_call"]
    assert len(calls) == 1 and calls[0]["result"] == "out"


@pytest.mark.parametrize("chunk", [1, 3, 7])
def test_live_replay_matches_one_whole_read(tmp_path: Path, monkeypatch, chunk: int):
    """The design's core claim: following a conversation tick by tick lands
    in exactly the same place as opening it once at the end.

    Replays a whole conversation ``chunk`` lines at a time — seams land
    mid-message at chunk sizes that do not divide a message's block count —
    and compares the accumulated entries against a single read of the
    finished file. A message split across a seam shows up as a duplicated
    reply; a dropped seam shows up as a missing one. The same check against
    a real 19 MB session on the dev box replayed 43 ticks with no drift.
    """
    monkeypatch.setattr(st, "WINDOW_BYTES", 400)
    rows = _conversation(12)
    path = tmp_path / "t.jsonl"
    path.write_bytes(b"")
    settled, tail, size = _live_view(path)
    pending: List[Dict[str, Any]] = []
    for i in range(0, len(rows), chunk):
        _append_jsonl(path, rows[i:i + chunk])
        out = st.transcript_tail(path, after=tail, size=size)
        assert out["reset"] is False
        if out["changed"]:
            settled = settled + out["entries"]
            pending = out["pending"]
            tail, size = out["tail"], out["size"]
    reference = st.transcript_page(path, limit=1000)["entries"]
    assert _turn_texts(settled + pending) == _turn_texts(reference)
    assert _kinds(settled + pending) == _kinds(reference)


class TestTranscriptTailEndpoint:
    """The forward cursor over the wire (#1050). Deliberately the *same*
    route as the backwards page — a new path shape would need its own
    ``_TERMINAL_GUARD_RULES`` row, and this is the same resource, caller and
    gate, read from the other end of the file."""

    def _served(self, overrides, monkeypatch, tmp_path, rows):
        overrides["session"].list_sessions.return_value = [_live()]
        path = _write_jsonl(tmp_path / "t.jsonl", rows)
        monkeypatch.setattr(
            board, "state_row_for_session",
            lambda live, rows_, sid: {"transcript_path": str(path)},
        )
        return path

    def test_page_hands_back_a_cursor_the_tail_resumes_from(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path
    ):
        client, _, overrides = webapp_client
        path = self._served(overrides, monkeypatch, tmp_path, _conversation(2))
        page = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert isinstance(page["tail"], int) and isinstance(page["size"], int)

        _append_jsonl(path, [_user("something new")])
        body = client.get(
            f"/api/claude-code/sessions/s1/transcript"
            f"?after={page['tail']}&size={page['size']}"
        ).json()
        assert body["available"] is True and body["changed"] is True
        texts = [e.get("text") for e in body["entries"] + body["pending"]]
        assert "something new" in texts

    def test_unchanged_file_answers_changed_false(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path
    ):
        client, _, overrides = webapp_client
        self._served(overrides, monkeypatch, tmp_path, _conversation(2))
        page = client.get("/api/claude-code/sessions/s1/transcript").json()
        body = client.get(
            f"/api/claude-code/sessions/s1/transcript"
            f"?after={page['tail']}&size={page['size']}"
        ).json()
        assert body["changed"] is False
        assert body["entries"] == [] and body["pending"] == []

    def test_before_and_after_together_are_refused(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path
    ):
        client, _, overrides = webapp_client
        self._served(overrides, monkeypatch, tmp_path, _conversation(2))
        res = client.get("/api/claude-code/sessions/s1/transcript?before=10&after=10")
        assert res.status_code == 400

    def test_a_dead_session_is_told_so_rather_than_polled_forever(
        self, webapp_client, _bypass_gate
    ):
        """What stops the client's timer: the same `session_not_found` the
        page uses, so a session that exits ends the refresh instead of
        leaving it ticking against nothing."""
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = []
        body = client.get(
            "/api/claude-code/sessions/s1/transcript?after=0&size=0"
        ).json()
        assert body["available"] is False and body["reason"] == "session_not_found"

    def test_read_failure_is_reported_not_swallowed(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path
    ):
        from app.webapp.routers import session_transcript as router_mod

        client, _, overrides = webapp_client
        self._served(overrides, monkeypatch, tmp_path, _conversation(2))

        def boom(*args, **kwargs):
            raise PermissionError("locked")

        monkeypatch.setattr(router_mod, "transcript_tail", boom)
        body = client.get(
            "/api/claude-code/sessions/s1/transcript?after=0&size=0"
        ).json()
        assert body["available"] is False and body["reason"] == "read_failed"

    def test_an_idle_tick_never_logs(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path, caplog
    ):
        """A tick runs every few seconds for as long as someone is reading.
        An info line per tick would bury the breadcrumbs that matter under
        its own traffic, so the quiet path stays quiet."""
        client, _, overrides = webapp_client
        self._served(overrides, monkeypatch, tmp_path, _conversation(2))
        page = client.get("/api/claude-code/sessions/s1/transcript").json()
        with caplog.at_level(logging.INFO, logger="app.webapp.routers.session_transcript"):
            for _ in range(5):
                client.get(
                    f"/api/claude-code/sessions/s1/transcript"
                    f"?after={page['tail']}&size={page['size']}"
                )
        assert caplog.records == []
