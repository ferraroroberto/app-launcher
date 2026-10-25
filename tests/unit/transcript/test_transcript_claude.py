"""Coding-tab transcript: Claude entry grammar, paging, byte caps and full-text reads."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.session_view import transcript as st
from src.transcript_flavors import _shared as tf_shared

from tests.unit.transcript._support import (
    _assistant,
    _codex,
    _conversation,
    _kinds,
    _parse_whole,
    _tail_of,
    _tool_result,
    _tool_use,
    _user,
    _walk,
    _write_jsonl,
)


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
    assert call["action"] == {"verb": "ran", "command": "pytest -q"}   # plain words (#1266)
    assert call["result"] == "3 passed"
    assert call["result_truncated"] is False
    sidechain = entries[7]
    assert sidechain["sidechain"] is True and sidechain["text"] == "Sub-agent says hi"
    assert entries[9]["text"] == "Look at this\n\n[image]"
    assert all(e["timestamp"] for e in entries)


def test_claude_entries_caps_long_bodies(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(tf_shared, "ASSISTANT_TEXT_CAP", 20)
    monkeypatch.setattr(tf_shared, "TOOL_RESULT_CAP", 5)
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _assistant([{"type": "text", "text": "x" * 15}], "m1"),
        _assistant([{"type": "text", "text": "y" * 15}], "m1"),
        _assistant([_tool_use("Read", {"file_path": "a.py"}, "t1")], "m1"),
        _user([_tool_result("t1", [{"type": "text", "text": "0123456789"}])]),
    ])
    entries = _parse_whole(path)
    assert entries[0]["truncated"] is True and len(entries[0]["text"]) == 20
    assert entries[1]["result"] == "01234" and entries[1]["result_truncated"] is True


def _queue_op(operation: str, text: str) -> Dict[str, Any]:
    return {"type": "queue-operation", "operation": operation, "content": text,
            "timestamp": "2026-09-14T10:00:02Z"}


def _queued_command(prompt: Any, **attachment: Any) -> Dict[str, Any]:
    """The line Claude writes when a mid-turn message is delivered (#1674)."""
    body = {"type": "queued_command", "prompt": prompt, "commandMode": "prompt",
            "origin": {"kind": "human"}}
    body.update(attachment)
    return {"type": "attachment", "timestamp": "2026-09-14T10:00:03Z", "attachment": body}


def test_mid_turn_message_renders_once_as_a_user_entry_in_place(tmp_path: Path):
    """#1674: a message typed while Claude is mid-turn is recorded as
    queue-operation enqueue/remove lines plus a ``queued_command`` attachment,
    never a ``user`` line — the attachment is the delivery moment and the one
    line the Chat view draws."""
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _user("start the work"),
        _assistant([_tool_use("Read", {"file_path": "a.py"}, "t1")], "m1"),
        _queue_op("enqueue", "also check b.py"),
        _queued_command("also check b.py"),
        _queue_op("remove", "also check b.py"),
        _user([_tool_result("t1", [{"type": "text", "text": "ok"}])]),
        _assistant([{"type": "text", "text": "done"}], "m2"),
    ])
    entries = _parse_whole(path)
    assert _kinds(entries) == ["user", "tool_call", "user", "assistant"]
    queued = entries[2]
    assert queued["text"] == "also check b.py"
    assert queued["timestamp"] == "2026-09-14T10:00:03Z"
    assert [e["text"] for e in entries if e["kind"] == "user"].count("also check b.py") == 1


def test_queued_command_block_prompt_and_non_human_deliveries(tmp_path: Path):
    """A block-list prompt joins its text; a background-task notification, a
    coordinator/meta delivery and an empty prompt are not typed messages."""
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _queued_command([{"type": "text", "text": "first"}, {"type": "text", "text": "second"}]),
        _queued_command("<task-notification>done</task-notification>", commandMode="task-notification"),
        _queued_command("coordinator note", isMeta=True, origin={"kind": "coordinator"}),
        _queued_command("   "),
    ])
    entries = _parse_whole(path)
    assert [(e["kind"], e["text"]) for e in entries] == [("user", "first\n\nsecond")]


def test_claude_error_result_is_marked_and_a_clean_one_is_not(tmp_path: Path):
    """Claude states the outcome outright, so both polarities are pinned
    (#1020). The flag is written **only** when true: its absence has to keep
    meaning "not reported as failed" for the flavours that cannot tell, so a
    reader must never emit ``error: False`` as a success claim."""
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _assistant([_tool_use("Read", {"file_path": "a.md"}, "t1")], msg_id="m1"),
        _user([dict(_tool_result("t1", "file body"))]),
        _assistant([_tool_use("Read", {"file_path": "gone.md"}, "t2")], msg_id="m2"),
        _user([dict(_tool_result("t2", "File does not exist."), is_error=True)]),
    ])
    entries = _parse_whole(path)
    assert _kinds(entries) == ["tool_call", "tool_call"]
    assert "error" not in entries[0]
    assert entries[1]["error"] is True and entries[1]["result"] == "File does not exist."


def test_orphan_error_result_keeps_its_flag(tmp_path: Path):
    """A failed result whose call fell off the page stands alone — and is
    still marked, or the page would show the failure as ordinary output."""
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _user([dict(_tool_result("ghost", "boom"), is_error=True)]),
    ])
    entries = _parse_whole(path)
    assert _kinds(entries) == ["tool_result"]
    assert entries[0]["error"] is True


def test_codex_failure_is_left_unmarked_not_guessed(tmp_path: Path):
    """Codex records no outcome field, so the reader marks nothing (#1020).

    A failing call and a working one differ only in the English word inside
    ``output`` — "Script failed" vs "Script completed" — and reading that
    would be a content-sniffing rule (the one exception is a rejected
    ``apply_patch``, whose own fixed message is read, #1356). The flavour
    declares ``partial``, which is what makes the client say "can't tell"
    rather than letting silence read as success.
    """
    path = _write_jsonl(tmp_path / "rollout.jsonl", [
        _codex({"type": "function_call", "name": "shell",
                "arguments": '{"command": "grep -r x ."}', "call_id": "c1"}),
        _codex({"type": "function_call_output", "call_id": "c1",
                "output": "Script failed\nOutput:\nrg: regex parse error"}),
    ])
    entries = _parse_whole(path, st.codex_entries)
    assert _kinds(entries) == ["tool_call"]
    assert "error" not in entries[0]
    assert "regex parse error" in entries[0]["result"]
    assert st.FLAVORS["codex"][2] == st.TOOL_ERRORS_PARTIAL


def test_every_flavour_declares_its_tool_error_fidelity():
    """The declaration rides in the ``FLAVORS`` row rather than in a table
    beside it, so a new harness cannot be added without answering "can this
    one tell a failed call from a working one?" — and cannot silently
    default to the answer that hides failures."""
    allowed = {st.TOOL_ERRORS_REPORTED, st.TOOL_ERRORS_PARTIAL, st.TOOL_ERRORS_NONE}
    for name, row in st.FLAVORS.items():
        assert len(row) == 3, name
        assert row[2] in allowed, (name, row[2])
    # Every agent the endpoint offers Chat mode for is covered.
    from app.webapp.routers.session_transcript import _FLAVOR_BY_AGENT
    assert set(_FLAVOR_BY_AGENT.values()) == set(st.FLAVORS)


def test_unknown_tool_result_stands_alone(tmp_path: Path):
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _user([_tool_result("ghost", "orphan output")]),
    ])
    entries = _parse_whole(path)
    assert _kinds(entries) == ["tool_result"]
    assert entries[0]["tool_use_id"] == "ghost" and entries[0]["text"] == "orphan output"


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
    assert joined == _parse_whole(path, tail=_tail_of(path, limit))


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
    assert page["entries"] == _parse_whole(path, tail=page["tail"])


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
    assert [e for page in reversed(pages) for e in page] == _parse_whole(
        path, tail=_tail_of(path, 100)
    )


def _screenshot_run(prefix: str, count: int, *, size: int = 1024 * 1024) -> List[Dict[str, Any]]:
    """``count`` tool calls whose results are each one ~``size``-byte line —
    the base64 image payload of a screenshot tool, the shape that filled
    #1120's pages with no conversation in them. Synthetic bytes only."""
    blob = "A" * size
    lines: List[Dict[str, Any]] = []
    for k in range(count):
        tid = f"{prefix}-{k}"
        lines.append(_assistant([_tool_use("Screenshot", {}, tid)], f"{prefix}-m{k}"))
        lines.append(_user([_tool_result(tid, [{
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": blob},
        }])]))
    return lines


def test_huge_tool_results_do_not_starve_a_page_of_turns(tmp_path: Path):
    """#1120: a few ~1 MB tool-result lines used to spend the whole 2 MiB
    budget, so a page stopped with zero conversation turns in it. A line is
    charged at most :data:`LINE_CHARGE_CAP` toward the budget, so one page
    reaches past them to the older turns."""
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _user("old prompt"),
        _assistant([{"type": "text", "text": "old reply"}], "m0"),
        *_screenshot_run("s", 6),
        _user("new prompt"),
        _assistant([{"type": "text", "text": "new reply"}], "m1"),
    ])
    page = st.transcript_page(path, limit=40)
    turns = [e["text"] for e in page["entries"] if st._is_turn(e)]
    assert turns == ["old prompt", "old reply", "new prompt", "new reply"]
    assert page["next_cursor"] is None


def test_real_bytes_read_stay_under_the_ceiling(tmp_path: Path, monkeypatch):
    """The charge cap must not make a page unbounded: a region of nothing
    but huge lines stops at :data:`REQUEST_READ_CEILING` real bytes with a
    cursor, and the walk from there still reaches every turn. (A page with
    no turn keeps its raw edge, so a call and its result may land on two
    pages — `_page`'s documented exception — hence turns, not entries.)"""
    monkeypatch.setattr(st, "REQUEST_READ_CEILING", 3 * 1024 * 1024)
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _user("first prompt"),
        _assistant([{"type": "text", "text": "first reply"}], "m0"),
        *_screenshot_run("s", 10),
    ])
    page = st.transcript_page(path, limit=40)
    assert page["next_cursor"] is not None
    assert not any(st._is_turn(e) for e in page["entries"])
    assert len(page["entries"]) < 10, "the ceiling must cut the run short"
    joined = [e for page in reversed(_walk(path, 40)) for e in page]
    assert [e["text"] for e in joined if st._is_turn(e)] == ["first prompt", "first reply"]
    assert sum(1 for e in joined if e["kind"] == "tool_call") == 10


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
    assert st.transcript_page(path) == {
        "entries": [], "next_cursor": None, "tool_errors": "reported",
        # An empty file still hands back a cursor a live view can resume
        # from, so the first turn of a just-started session appears (#1050).
        "tail": 0, "size": 0,
    }


def test_page_exposes_offset_on_turns_only(tmp_path: Path):
    """#985: a turn keeps its byte offset (the copy-full-text route's
    lookup key) — the folded kinds have it stripped.

    #1050 carved one exception, pinned below it: from the live-tail offset
    on, every kind keeps an offset, because a live view re-renders exactly
    that region and needs a stable key per card.
    """
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _user("hi"),
        _assistant([{"type": "thinking", "thinking": "hmm"}], "m1"),
        _assistant([{"type": "text", "text": "hello"}], "m1"),
        # A second message, so the first one is behind the live tail and
        # keeps the pre-#1050 shape.
        _user("more"),
        _assistant([{"type": "thinking", "thinking": "later"}], "m2"),
        _assistant([{"type": "text", "text": "done"}], "m2"),
    ])
    page = st.transcript_page(path)
    entries = page["entries"]
    tail = page["tail"]
    # Split where the client does: the first entry carrying an offset at or
    # past the tail opens the live region, and everything after it is in it.
    cut = next(i for i, e in enumerate(entries) if e.get("offset", -1) >= tail)
    settled, live = entries[:cut], entries[cut:]
    assert [e["kind"] for e in settled] == ["user", "thinking", "assistant", "user"]
    for e in settled:
        if e["kind"] in ("user", "assistant"):
            assert isinstance(e["offset"], int)
        else:
            assert "offset" not in e, "a settled folded entry keeps no offset"
    assert [e["kind"] for e in live] == ["thinking", "assistant"]
    for e in live:
        assert isinstance(e["offset"], int), "the live tail keys every kind"


# ---------------------------------------------------------- entry_full_text


def test_entry_full_text_user_is_uncapped(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(tf_shared, "USER_TEXT_CAP", 5)
    path = _write_jsonl(tmp_path / "t.jsonl", [_user("a long prompt that would be capped")])
    page = st.transcript_page(path)
    entry = page["entries"][0]
    assert entry["truncated"] is True
    full = st.entry_full_text(path, entry["offset"], "claude")
    assert full == {"text": "a long prompt that would be capped", "truncated": False}


def test_entry_full_text_merges_assistant_blocks_across_lines(tmp_path: Path, monkeypatch):
    """The page-serving reader caps an assistant reply at 20 chars here; the
    full-text route reconstructs both merged text blocks — with a tool call
    and its result (different lines, different kinds) sitting between them,
    same shape :func:`_drop_partial_leading_message` documents."""
    monkeypatch.setattr(tf_shared, "ASSISTANT_TEXT_CAP", 20)
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _assistant([{"type": "text", "text": "x" * 15}], "m1"),
        _assistant([_tool_use("Bash", {"command": "ls"}, "t1")], "m1"),
        _user([_tool_result("t1", "listing")]),
        _assistant([{"type": "text", "text": "y" * 15}], "m1"),
    ])
    page = st.transcript_page(path)
    entry = next(e for e in page["entries"] if e["kind"] == "assistant")
    assert entry["truncated"] is True and len(entry["text"]) == 20
    full = st.entry_full_text(path, entry["offset"], "claude")
    assert full == {"text": "x" * 15 + "\n\n" + "y" * 15, "truncated": False}


def test_entry_full_text_codex_assistant(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(tf_shared, "ASSISTANT_TEXT_CAP", 5)
    path = _write_jsonl(tmp_path / "rollout.jsonl", [
        _codex({"type": "message", "role": "assistant",
               "content": [{"type": "output_text", "text": "a whole answer"}]}),
    ])
    page = st.transcript_page(path, flavor="codex")
    entry = page["entries"][0]
    assert entry["truncated"] is True
    full = st.entry_full_text(path, entry["offset"], "codex")
    assert full == {"text": "a whole answer", "truncated": False}


def test_entry_full_text_unknown_offset_is_none(tmp_path: Path):
    path = _write_jsonl(tmp_path / "t.jsonl", [_user("hi")])
    assert st.entry_full_text(path, 3, "claude") is None       # mid-line, no turn starts there
    assert st.entry_full_text(path, 9_999, "claude") is None   # past EOF
