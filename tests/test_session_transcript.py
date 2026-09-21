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
  * ``session_transcript.grok_entries`` — the Grok Build ``updates.jsonl``
    grammar (#1012), driven by a captured session: chunk kinds, tool calls
    paired with their completion, the three result shapes, epoch stamps
    normalised to ISO, and consecutive-only chunk coalescing.
  * ``session_transcript.pi_entries`` — the Pi session JSONL grammar
    (#1013), driven by a captured session: tool calls as assistant content
    blocks paired with their own ``toolResult`` message, failed results,
    several entries sharing one line's offset, the encrypted thinking
    signature never shown, and ``custom`` records dropped so a tool result
    is not counted twice.
  * ``session_transcript.antigravity_entries`` — the Antigravity CLI
    conversation-log grammar (#1014), driven by a captured session: a tool
    call riding on a contentless ``PLANNER_RESPONSE`` paired FIFO with the
    next ``MODEL`` step (whose type does not name the tool), the timing
    header dropped, the ``<USER_REQUEST>`` wrapper unwrapped, harness
    plumbing folded, and the flat file's self-truncation and
    double-encoded arguments undone.
  * ``board_exchange.find_pi_transcript`` — the Pi source correlation: the
    state row's own key names the file, ambiguity answers ``None``.
  * ``board_exchange.find_antigravity_transcript`` — the Antigravity source
    correlation: the harness's newest-conversation-per-folder cache, refused
    when a second live session shares the folder or nothing was written
    since this session started.
  * ``board_exchange.find_claude_transcript`` — the Claude fallback for a
    live session the hook left no row for (#1023): the newest conversation
    in the cwd's own project folder, matched case-insensitively, refused on
    the same two guards.
  * ``GET /api/claude-code/sessions/{sid}/transcript`` — passkey-gated,
    distinct unavailable reasons, no transcript bodies in the log.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

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


def _lines_of(path: Path) -> List[Any]:
    """Every non-blank line of ``path`` as the ``(byte offset, text)`` pairs
    the entry builders take."""
    raw = path.read_bytes()
    lines, pos = [], 0
    for chunk in raw.split(b"\n"):
        if chunk.strip():
            lines.append((pos, chunk.decode("utf-8", errors="replace")))
        pos += len(chunk) + 1
    return lines


def _parse_whole(
    path: Path, build=st.claude_entries, *, tail: int = -1
) -> List[Dict[str, Any]]:
    """Reference: one parse of the whole file — offset stripped from the
    folded kinds, kept on turns, matching :func:`session_transcript._page`
    (#985).

    ``tail`` is the live-tail offset of the page(s) being compared (#1050):
    from there on a folded entry keeps its offset too, because that region
    is what a live view re-renders and it needs a key per card. Default -1
    means "no page ended at EOF", i.e. strip them all.
    """
    entries = build(_lines_of(path))
    for e in entries:
        if not st._is_turn(e) and not (tail >= 0 and e.get("offset", -1) >= tail):
            e.pop("offset", None)
    return entries


def _tail_of(path: Path, limit: int, flavor: str = "claude") -> int:
    """The newest page's live-tail offset (#1050) — where its provisional
    region begins, and so which entries keep a folded offset."""
    return st.transcript_page(path, limit=limit, flavor=flavor)["tail"]


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
    would be the first content-sniffing rule in any reader here. The flavour
    declares ``none`` instead, which is what makes the client say "can't
    tell" rather than letting silence read as success.
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
    assert st.FLAVORS["codex"][2] == st.TOOL_ERRORS_NONE


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
    monkeypatch.setattr(st, "USER_TEXT_CAP", 5)
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
    monkeypatch.setattr(st, "ASSISTANT_TEXT_CAP", 20)
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
    monkeypatch.setattr(st, "ASSISTANT_TEXT_CAP", 5)
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


# ---------------------------------------------------------- grok_entries

# The captured Grok Build session (#1012): two probe prompts were text-only,
# so this one was recorded specifically to pin the two shapes they missed —
# a read-only tool call with its result, and a long multi-paragraph reply.
# Redacted (home paths as `~`), never hand-written.
GROK_CAPTURE = Path(__file__).parent / "fixtures" / "grok_updates.jsonl"


def _grok(update: Dict[str, Any], *, ts: int = 1_789_670_620, **meta: Any) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "timestamp": ts, "method": "session/update",
        "params": {"sessionId": "s-1", "update": update, "_meta": dict(meta)},
    }
    return row


def _grok_chunk(kind: str, text: str, **kw: Any) -> Dict[str, Any]:
    return _grok({"sessionUpdate": kind, "content": {"type": "text", "text": text}}, **kw)


def test_grok_entries_grammar_from_capture():
    """The real capture, parsed whole: the grammar and the turn order."""
    entries = _parse_whole(GROK_CAPTURE, st.grok_entries)
    assert _kinds(entries) == [
        "user", "thinking", "assistant", "tool_call", "thinking", "assistant",
        "user", "tool_call", "assistant",          # the failing read (#1020)
    ]
    assert entries[0]["text"].startswith("Use exactly one read-only tool call")
    # The tool call: name from `title`, summary from `rawInput`, and the
    # result text recovered although this tool emitted no ACP `content`.
    assert entries[3]["name"] == "list_dir"
    assert entries[3]["summary"].endswith("docs")
    assert "architecture.mmd" in entries[3]["result"]
    # `hook_execution` and `turn_completed` carry no text and are dropped.
    assert len(entries) == 9


def test_grok_message_runs_either_side_of_a_tool_stay_separate():
    """The finding that decided the coalescing rule (#1012).

    One turn wrote two ``agent_message_chunk`` lines: a preamble *before*
    the tool call and the real answer *after* it. Merging a turn's chunks
    would hoist the answer above the tool call that produced it, so only
    consecutive chunks merge — these two must stay two entries, in order.
    """
    entries = _parse_whole(GROK_CAPTURE, st.grok_entries)
    # The first turn only — the capture gained a second exchange with #1020's
    # failing read, whose own reply would otherwise be counted here.
    first_turn = entries[:6]
    assistants = [e for e in first_turn if e["kind"] == "assistant"]
    assert len(assistants) == 2
    assert assistants[0]["text"].startswith("I'll list the `docs` folder")
    assert assistants[1]["text"].startswith("The `docs` folder")
    # …and the tool call sits between them, not after both.
    assert _kinds(first_turn).index("tool_call") == 3


def test_grok_long_reply_arrives_as_one_chunk():
    """Grok folds the stream itself: a three-paragraph reply is one line.

    This is what the #989 probe recorded as unknown. Measured here, not
    assumed — `chunkId` counts the stream chunks Grok merged before
    writing, so a high count on a single line is the evidence.
    """
    rows = [json.loads(line) for line in GROK_CAPTURE.read_text(encoding="utf-8").splitlines() if line.strip()]
    replies = [r for r in rows
               if r["params"]["update"].get("sessionUpdate") == "agent_message_chunk"]
    # The long reply is the first turn's answer; the capture's later
    # exchange (#1020's failing read) ends in a one-line reply.
    answer = replies[1]
    text = answer["params"]["update"]["content"]["text"]
    assert text.count("\n\n") >= 2 and len(text) > 2_000   # three paragraphs, one line
    assert answer["params"]["_meta"]["chunkId"] > 100      # …folded from many stream chunks


def test_grok_consecutive_chunks_of_one_kind_coalesce(tmp_path: Path):
    """The safety net: adjacent same-kind chunks merge, keeping the first
    offset so a page cursor can never split a coalesced turn."""
    path = _write_jsonl(tmp_path / "updates.jsonl", [
        _grok_chunk("user_message_chunk", "ask"),
        _grok_chunk("agent_message_chunk", "part one"),
        _grok_chunk("agent_message_chunk", "part two"),
    ])
    entries = _parse_whole(path, st.grok_entries)
    assert _kinds(entries) == ["user", "assistant"]
    assert entries[1]["text"] == "part one\n\npart two"
    # The *first* chunk's offset — the byte the cursor contract hands back —
    # not the second's, or a cursor could land inside the coalesced turn.
    first_reply_offset = len(path.read_bytes().split(b"\n")[0]) + 1
    assert entries[1]["offset"] == first_reply_offset


def test_grok_timestamps_are_iso_not_raw_epoch(tmp_path: Path):
    """Grok stamps in epoch seconds/ms; the client renders with
    ``new Date(ts)``, which reads a bare number as milliseconds — a raw
    epoch-seconds value would show 1970. Every flavour emits ISO."""
    path = _write_jsonl(tmp_path / "updates.jsonl", [
        _grok_chunk("user_message_chunk", "ask", agentTimestampMs=1_789_670_620_500),
        _grok_chunk("agent_message_chunk", "reply", ts=1_789_670_621),
    ])
    entries = _parse_whole(path, st.grok_entries)
    assert entries[0]["timestamp"] == "2026-09-17T18:43:40.500000Z"   # ms wins
    assert entries[1]["timestamp"] == "2026-09-17T18:43:41Z"          # seconds fallback
    # A record with no usable stamp says so rather than inventing one.
    bare = _write_jsonl(tmp_path / "bare.jsonl", [
        {"method": "session/update",
         "params": {"update": {"sessionUpdate": "user_message_chunk",
                               "content": {"type": "text", "text": "x"}}}},
    ])
    assert _parse_whole(bare, st.grok_entries)[0]["timestamp"] is None


def test_grok_tool_result_sources_and_textless_completion(tmp_path: Path):
    """Three measured result shapes, in the order the reader tries them."""
    path = _write_jsonl(tmp_path / "updates.jsonl", [
        _grok({"sessionUpdate": "tool_call", "toolCallId": "t1",
               "title": "read_file", "rawInput": {"target_file": "~/x.md"}}),
        # 1. ACP content blocks (ReadFile, GrepSearch carried these).
        _grok({"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed",
               "content": [{"type": "content", "content": {"type": "text", "text": "file body"}}]}),
        _grok({"sessionUpdate": "tool_call", "toolCallId": "t2",
               "title": "list_dir", "rawInput": {"target_directory": "~/docs"}}),
        # 2. No content blocks — reach one level into `rawOutput` (ListDir).
        _grok({"sessionUpdate": "tool_call_update", "toolCallId": "t2", "status": "completed",
               "rawOutput": {"type": "ListDir", "Content": {"content": "- a.md\n- b.md"}}}),
        _grok({"sessionUpdate": "tool_call", "toolCallId": "t3",
               "title": "scheduler_list", "rawInput": {}}),
        # 3. Neither — say which output type it was, never an empty result.
        _grok({"sessionUpdate": "tool_call_update", "toolCallId": "t3", "status": "completed",
               "rawOutput": {"type": "SchedulerList", "tasks": []}}),
    ])
    entries = _parse_whole(path, st.grok_entries)
    assert _kinds(entries) == ["tool_call", "tool_call", "tool_call"]
    assert entries[0]["result"] == "file body"
    assert entries[1]["result"] == "- a.md\n- b.md"
    assert entries[2]["result"] == "(SchedulerList: no text output)"


def test_grok_failed_call_keeps_its_result_and_is_marked(tmp_path: Path):
    """The regression this fixes twice over (#1020).

    Grok reports a failed tool call as ``status: "failed"`` — a value the
    reader had never seen, because the 19 ``tool_call_update`` records on
    disk when it was written were all ``completed``. It therefore attached a
    result only on ``completed``, so a *failed* call's result was dropped
    entirely and rendered "no result on this page": the error text the user
    most needed was the one thing the page could not show.
    """
    entries = _parse_whole(GROK_CAPTURE, st.grok_entries)
    failed = entries[7]
    assert failed["kind"] == "tool_call" and failed["name"] == "read_file"
    assert failed["result"].startswith("Error: ")
    assert failed["error"] is True
    # The working call in the same capture stays unmarked.
    assert "error" not in entries[3]


def test_grok_in_flight_status_never_occupies_the_result_slot(tmp_path: Path):
    """``pending``/``in_progress`` are progress, not results. Attaching one
    would fill the call's single result slot and lock the real result out —
    the reason the status set is an explicit pair rather than "anything but
    the status-less half"."""
    path = _write_jsonl(tmp_path / "updates.jsonl", [
        _grok({"sessionUpdate": "tool_call", "toolCallId": "t1",
               "title": "read_file", "rawInput": {"target_file": "~/x.md"}}),
        _grok({"sessionUpdate": "tool_call_update", "toolCallId": "t1",
               "status": "in_progress",
               "content": [{"type": "content",
                            "content": {"type": "text", "text": "reading…"}}]}),
        _grok({"sessionUpdate": "tool_call_update", "toolCallId": "t1",
               "status": "failed",
               "content": [{"type": "content",
                            "content": {"type": "text", "text": "boom"}}]}),
    ])
    entries = _parse_whole(path, st.grok_entries)
    assert len(entries) == 1
    assert entries[0]["result"] == "boom" and entries[0]["error"] is True


def test_grok_status_less_tool_update_is_not_a_result(tmp_path: Path):
    """Each call gets two ``tool_call_update`` records — a metadata one
    that only restates the call, then the completion. Only the second is a
    result, or every call would show its own arguments as its output."""
    path = _write_jsonl(tmp_path / "updates.jsonl", [
        _grok({"sessionUpdate": "tool_call", "toolCallId": "t1",
               "title": "list_dir", "rawInput": {"target_directory": "~/docs"}}),
        _grok({"sessionUpdate": "tool_call_update", "toolCallId": "t1", "kind": "other",
               "title": "List `~/docs`", "locations": [{"path": "~/docs"}],
               "rawInput": {"variant": "ListDir"}}),
    ])
    entries = _parse_whole(path, st.grok_entries)
    assert len(entries) == 1 and entries[0]["result"] is None


def _grok_conversation(turns: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for i in range(turns):
        rows.append(_grok_chunk("user_message_chunk", f"ask {i}"))
        rows.append(_grok_chunk("agent_thought_chunk", f"thinking about {i} " * 3))
        rows.append(_grok({"sessionUpdate": "tool_call", "toolCallId": f"t{i}",
                           "title": "grep", "rawInput": {"pattern": f"p{i}"}}))
        rows.append(_grok({"sessionUpdate": "tool_call_update", "toolCallId": f"t{i}",
                           "status": "completed",
                           "content": [{"type": "content",
                                        "content": {"type": "text", "text": "hit " * (i % 7)}}]}))
        rows.append(_grok_chunk("agent_message_chunk", f"answer {i}"))
        rows.append(_grok({"sessionUpdate": "turn_completed", "stop_reason": "end_turn"}))
    return rows


def test_grok_pages_concatenate_across_window_boundaries(tmp_path: Path, monkeypatch):
    """Every page, oldest-first, equals one whole parse — nothing split or
    duplicated when the window edge lands mid-turn."""
    monkeypatch.setattr(st, "WINDOW_BYTES", 400)
    path = _write_jsonl(tmp_path / "updates.jsonl", _grok_conversation(20))
    pages = _walk(path, 4, flavor="grok")
    assert len(pages) > 1
    assert [e for page in reversed(pages) for e in page] == _parse_whole(path, st.grok_entries)


def test_grok_entry_full_text_is_uncapped(tmp_path: Path, monkeypatch):
    """A truncated turn reads back whole — what Chat's copy (#985) and
    read-aloud (#988) call when a page entry came back ``truncated``."""
    monkeypatch.setattr(st, "ASSISTANT_TEXT_CAP", 40)
    long_reply = "paragraph one. " * 40
    path = _write_jsonl(tmp_path / "updates.jsonl", [
        _grok_chunk("user_message_chunk", "ask"),
        _grok_chunk("agent_message_chunk", long_reply),
    ])
    page = st.transcript_page(path, flavor="grok")
    reply = [e for e in page["entries"] if e["kind"] == "assistant"][0]
    assert reply["truncated"] is True and len(reply["text"]) == 40
    full = st.entry_full_text(path, reply["offset"], "grok")
    assert full["text"] == long_reply.strip() and full["truncated"] is False
    assert st.entry_full_text(path, 3, "grok") is None       # mid-line
    assert st.entry_full_text(path, 99_999, "grok") is None  # past EOF


# ------------------------------------------------------------ pi_entries

# A Pi session captured for #1013 by launching Pi through the launcher's own
# API and driving it over `/input`, so the correlation below is the real one.
# Two turns: a read-only tool call whose result succeeds, and one whose
# result fails (`isError: true`) — plus the `quit`/`Goodbye.` pair the
# launcher's Stop leaks into the history, left in on purpose (the parser must
# not special-case it; it is a stop-path bug tracked separately).
# Redacted: the working directory becomes `E:\work\project`, and the
# `thinkingSignature` / `textSignature` blobs are trimmed to 80 chars.
PI_CAPTURE = Path(__file__).parent / "fixtures" / "pi_session.jsonl"


def _pi_msg(role: str, content: Any, mid: str = "m1", **extra: Any) -> Dict[str, Any]:
    message: Dict[str, Any] = {"role": role, "content": content}
    message.update(extra)
    return {"type": "message", "id": mid, "timestamp": "2026-09-17T19:41:00.000Z",
            "message": message}


def _pi_text(text: str) -> Dict[str, Any]:
    return {"type": "text", "text": text}


def test_pi_entries_grammar_from_capture():
    """The real capture, parsed whole: the grammar and the turn order.

    The shape the issue guessed at is wrong in two ways this pins — a tool
    call is an *assistant content block*, and its result is a separate
    message with ``role: "toolResult"``, not a block of the user turn.
    """
    entries = _parse_whole(PI_CAPTURE, st.pi_entries)
    assert _kinds(entries) == [
        "system", "system",                              # model + thinking level
        "user", "thinking", "tool_call", "thinking", "assistant",
        "user", "tool_call", "assistant",
        "user", "assistant",                             # the leaked Stop turn
    ]
    assert entries[0]["text"] == "openai-codex gpt-5.6-sol"
    assert entries[2]["text"].startswith("List the files in the docs folder")
    # The tool call: name and arguments from the assistant block, result
    # attached from the separate `toolResult` message via `toolCallId`.
    assert entries[4]["name"] == "bash"
    assert entries[4]["summary"].startswith("find docs")
    assert "architecture.mmd" in entries[4]["result"]
    assert entries[6]["text"].count("\n\n") >= 2         # the multi-paragraph reply


def test_pi_one_line_carries_a_whole_message():
    """Pi puts a message's entire content list on one line, so several
    entries share its byte offset — unlike Claude's one-block-per-line.

    The consequence that matters: exactly one *turn* entry per offset, which
    is what ``entry_full_text`` resolves against.
    """
    entries = _parse_whole(PI_CAPTURE, st.pi_entries)
    thinking, reply = entries[5], entries[6]
    assert thinking["kind"] == "thinking" and reply["kind"] == "assistant"
    # Same record: the folded kinds have their offset stripped, so compare
    # against the raw parse instead.
    raw = st.pi_entries(_lines_of(PI_CAPTURE))
    at_offset = [e for e in raw if e["offset"] == reply["offset"]]
    assert _kinds(at_offset) == ["thinking", "assistant"]
    assert sum(1 for e in at_offset if st._is_turn(e)) == 1


def test_pi_failed_tool_result_pairs_and_is_marked():
    """A failed call (``isError: true``) attaches like any other *and*
    carries the flag (#1020). The capture holds both polarities — the
    ``bash`` call succeeded with ``isError: false`` — so this pins that the
    flag follows the record rather than being set for every result."""
    entries = _parse_whole(PI_CAPTURE, st.pi_entries)
    failed = entries[8]
    assert failed["kind"] == "tool_call" and failed["name"] == "read"
    assert failed["result"].startswith("ENOENT: no such file or directory")
    assert failed["error"] is True
    assert "error" not in entries[2]           # `isError: false`, same capture


def test_pi_thinking_signature_never_reaches_an_entry():
    """``thinkingSignature`` is an encrypted blob many times the size of the
    thought it accompanies; only ``thinking`` is shown."""
    entries = _parse_whole(PI_CAPTURE, st.pi_entries)
    thoughts = [e for e in entries if e["kind"] == "thinking"]
    assert thoughts and all("TRIMMED" not in e["text"] for e in thoughts)
    assert thoughts[0]["text"] == "**Planning single read-only directory listing**"


def test_pi_timestamps_are_the_outer_iso_string(tmp_path: Path):
    """The trap the Grok flavour hit (#1012): the *inner* ``message
    .timestamp`` is epoch milliseconds, so reading it would render every
    turn as 1970 in the client's ``new Date(ts)``."""
    path = _write_jsonl(tmp_path / "pi.jsonl", [
        _pi_msg("user", [_pi_text("hi")], timestamp=1789674055249),
    ])
    assert _parse_whole(path, st.pi_entries)[0]["timestamp"] == "2026-09-17T19:41:00.000Z"


def test_pi_custom_tool_watch_records_are_dropped(tmp_path: Path):
    """``custom`` records restate a tool execution the ``toolResult``
    message already carries (228 of them in the on-disk corpus, all
    ``claude-agent-sdk-tool-watch``), so reading them would show every tool
    result twice."""
    path = _write_jsonl(tmp_path / "pi.jsonl", [
        _pi_msg("assistant", [{"type": "toolCall", "id": "t1", "name": "bash",
                               "arguments": {"command": "git branch"}}]),
        {"type": "custom", "customType": "claude-agent-sdk-tool-watch",
         "id": "c1", "timestamp": "2026-09-17T19:41:01.000Z",
         "data": {"type": "tool_execution_end", "toolCallId": "t1",
                  "toolName": "bash", "content": "main\n", "isError": False}},
        _pi_msg("toolResult", [_pi_text("main")], mid="m2",
                toolCallId="t1", toolName="bash", isError=False),
    ])
    entries = _parse_whole(path, st.pi_entries)
    assert _kinds(entries) == ["tool_call"]      # not ["tool_call", "tool_result"]
    assert entries[0]["result"] == "main"


def test_pi_image_tool_result_is_a_placeholder(tmp_path: Path):
    """An ``image`` block carries raw base64 in ``data`` (35 of them in the
    corpus, from screenshot tools) — counted, never decoded into text."""
    blob = "iVBORw0KGgoAAAANSUhEUg" * 200
    path = _write_jsonl(tmp_path / "pi.jsonl", [
        _pi_msg("assistant", [{"type": "toolCall", "id": "t1", "name": "screenshot",
                               "arguments": {"path": "shot.png"}}]),
        _pi_msg("toolResult", [_pi_text("captured"), {"type": "image", "data": blob}],
                mid="m2", toolCallId="t1", toolName="screenshot", isError=False),
    ])
    result = _parse_whole(path, st.pi_entries)[0]["result"]
    assert result == "captured\n\n[image]"
    assert "iVBORw0" not in result


def test_pi_skill_injection_and_compaction_fold_as_system(tmp_path: Path):
    """Pi's skill loader injects a whole SKILL.md as a user turn, and a
    ``compaction`` record summarises what was rewound — both are plumbing,
    folded so the chat reads as a plain you-to-agent exchange."""
    path = _write_jsonl(tmp_path / "pi.jsonl", [
        _pi_msg("user", [_pi_text('<skill name="issue-finish" location="~/skills">…</skill>')]),
        {"type": "compaction", "id": "k1", "timestamp": "2026-09-17T19:41:02.000Z",
         "summary": "## Goal\nShip the reader."},
        {"type": "session_info", "id": "s1", "timestamp": "2026-09-17T19:41:03.000Z",
         "name": "renamed"},
        _pi_msg("user", [_pi_text("a real prompt")], mid="m2"),
    ])
    entries = _parse_whole(path, st.pi_entries)
    assert _kinds(entries) == ["system", "system", "user"]   # session_info dropped
    assert entries[0]["label"] == "skill"
    assert entries[1]["label"] == "compaction"
    assert entries[2]["text"] == "a real prompt"


def _pi_conversation(turns: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = [
        {"type": "session", "version": 3, "id": "01a0", "timestamp": "2026-09-17T19:40:30.292Z",
         "cwd": r"E:\work\project"},
    ]
    for i in range(turns):
        rows.append(_pi_msg("user", [_pi_text(f"ask {i}")], mid=f"u{i}"))
        rows.append(_pi_msg("assistant", [
            {"type": "thinking", "thinking": f"weighing {i} " * 3,
             "thinkingSignature": "sig" * 40},
            {"type": "toolCall", "id": f"t{i}", "name": "bash",
             "arguments": {"command": f"echo {i}"}},
        ], mid=f"a{i}"))
        rows.append(_pi_msg("toolResult", [_pi_text(f"out {i}")], mid=f"r{i}",
                            toolCallId=f"t{i}", toolName="bash", isError=False))
        rows.append(_pi_msg("assistant", [_pi_text(f"answer {i}")], mid=f"z{i}"))
    return rows


def test_pi_pages_concatenate_across_window_boundaries(tmp_path: Path, monkeypatch):
    """Every page, oldest-first, equals one whole parse — nothing split or
    duplicated when the window edge lands mid-turn."""
    monkeypatch.setattr(st, "WINDOW_BYTES", 400)
    path = _write_jsonl(tmp_path / "pi.jsonl", _pi_conversation(20))
    pages = _walk(path, 4, flavor="pi")
    assert len(pages) > 1
    assert [e for page in reversed(pages) for e in page] == _parse_whole(path, st.pi_entries)


def test_pi_entry_full_text_is_uncapped(tmp_path: Path, monkeypatch):
    """A truncated turn reads back whole — what Chat's copy (#985) and
    read-aloud (#988) call when a page entry came back ``truncated``."""
    monkeypatch.setattr(st, "ASSISTANT_TEXT_CAP", 40)
    long_reply = "paragraph one. " * 40
    path = _write_jsonl(tmp_path / "pi.jsonl", [
        _pi_msg("user", [_pi_text("ask")]),
        _pi_msg("assistant", [
            {"type": "thinking", "thinking": "brief", "thinkingSignature": "sig" * 40},
            _pi_text(long_reply),
        ], mid="m2"),
    ])
    page = st.transcript_page(path, flavor="pi")
    reply = [e for e in page["entries"] if e["kind"] == "assistant"][0]
    assert reply["truncated"] is True and len(reply["text"]) == 40
    full = st.entry_full_text(path, reply["offset"], "pi")
    assert full["text"] == long_reply.strip() and full["truncated"] is False
    assert st.entry_full_text(path, 3, "pi") is None       # mid-line
    assert st.entry_full_text(path, 99_999, "pi") is None  # past EOF


# --------------------------------------------------- antigravity_entries


# An Antigravity CLI session captured for #1014 by launching it through the
# launcher's own API from this repo's folder and driving it over `/input`,
# so the correlation below is the real one. Two turns — a read-only
# `list_dir` and a `run_command` made to fail on purpose — plus the
# `quit`/`Goodbye!` pair the launcher's Stop leaks into the history (#1016),
# left in on purpose: the parser must not special-case it.
# Redacted: the working directory becomes `E:\work\project`, and each
# `thinking` block is trimmed to 240 chars.
AGY_CAPTURE = Path(__file__).parent / "fixtures" / "antigravity_transcript.jsonl"


def _agy(step: int, source: str, step_type: str, **extra: Any) -> Dict[str, Any]:
    row = {"step_index": step, "source": source, "type": step_type,
           "status": "DONE", "created_at": "2026-09-17T21:08:20Z"}
    row.update(extra)
    return row


def _agy_prompt(text: str, step: int = 0) -> Dict[str, Any]:
    return _agy(step, "USER_EXPLICIT", "USER_INPUT", content=(
        f"<USER_REQUEST>\n{text}\n</USER_REQUEST>\n"
        "<ADDITIONAL_METADATA>\nThe current local time is: 2026-09-17T23:08:20+02:00.\n"
        "</ADDITIONAL_METADATA>"
    ))


def test_antigravity_entries_grammar_from_capture():
    """The real capture, parsed whole: the grammar and the turn order.

    Pins the two shapes the issue could not know without a capture — a tool
    call rides on the ``PLANNER_RESPONSE`` that has no ``content`` of its
    own, and its result is the next ``MODEL`` step whose type does *not*
    name the tool (``list_dir`` answered as ``GENERIC``).
    """
    entries = _parse_whole(AGY_CAPTURE, st.antigravity_entries)
    assert _kinds(entries) == [
        "user", "thinking", "tool_call", "thinking", "assistant",
        "user", "thinking", "tool_call", "assistant",
        "user", "thinking", "assistant",          # the leaked Stop turn (#1016)
        "user", "thinking", "tool_call", "assistant",   # the ERROR step (#1020)
    ]
    assert entries[0]["text"].startswith("List the files in the current folder")
    # The tool call: name from the step's `tool_calls`, summary from its
    # first real argument (not the `toolAction`/`toolSummary` UI labels),
    # result attached from the *following* step.
    assert entries[2]["name"] == "list_dir"
    assert entries[2]["summary"] == r"E:\work\project"
    assert entries[2]["result"].startswith('{"name":".agents"')
    # The harness's own timing header never reaches the card.
    assert "Created At:" not in entries[2]["result"]
    assert entries[4]["text"].count("\n\n") >= 2          # the multi-paragraph reply
    # Antigravity's two failure shapes, the reason this flavour is
    # `partial` (#1020). A shell command exiting non-zero is `status:
    # "DONE"` — indistinguishable from a working call except in English
    # prose, so it is deliberately left unmarked...
    assert entries[7]["name"] == "run_command"
    assert "exited with code 1" in entries[7]["result"]
    assert "error" not in entries[7]
    # ...while a tool that could not run at all is `status: "ERROR"`, which
    # is structural and is marked.
    assert entries[14]["name"] == "view_file"
    assert entries[14]["error"] is True
    assert entries[9]["text"] == "quit"


def test_antigravity_user_prompt_shows_only_the_request():
    """A typed prompt arrives wrapped in ``<USER_REQUEST>`` with the local
    time — and sometimes a settings change — appended after it. Only the
    request is shown; an unwrapped step still renders rather than vanishing.
    """
    entries = st.antigravity_entries([
        (0, json.dumps(_agy(0, "USER_EXPLICIT", "USER_INPUT", content=(
            "<USER_REQUEST>\nsay hi\n</USER_REQUEST>\n"
            "<ADDITIONAL_METADATA>\nThe current local time is: 2026-09-17T23:08:20+02:00.\n"
            "</ADDITIONAL_METADATA>\n<USER_SETTINGS_CHANGE>\nThe user changed setting "
            "`Model Selection` from None to Gemini 3.1 Pro (Low).\n</USER_SETTINGS_CHANGE>"
        )))),
        (1, json.dumps(_agy(1, "USER_EXPLICIT", "USER_INPUT", content="bare prompt"))),
    ])
    assert [e["text"] for e in entries] == ["say hi", "bare prompt"]


def test_antigravity_tool_results_pair_positionally_and_orphans_stand_alone():
    """No call id exists anywhere, so calls and results pair FIFO — and a
    result arriving with nothing outstanding stands on its own rather than
    attaching to an unrelated call. (Six of the 210 logs on this box have a
    dropped step, which is exactly that case.)"""
    rows = [
        _agy(0, "MODEL", "PLANNER_RESPONSE", tool_calls=[
            {"name": "list_dir", "args": {"DirectoryPath": "a"}},
            {"name": "view_file", "args": {"AbsolutePath": "b"}},
        ]),
        _agy(1, "MODEL", "GENERIC", content="Created At: t\nCompleted At: t\nfirst"),
        _agy(2, "MODEL", "VIEW_FILE", content="Created At: t\nCompleted At: t\nsecond"),
        _agy(3, "MODEL", "RUN_COMMAND", content="Created At: t\norphan"),
    ]
    entries = st.antigravity_entries([(i * 10, json.dumps(r)) for i, r in enumerate(rows)])
    assert _kinds(entries) == ["tool_call", "tool_call", "tool_result"]
    assert [e["name"] for e in entries[:2]] == ["list_dir", "view_file"]
    assert entries[0]["result"] == "first" and entries[1]["result"] == "second"
    assert entries[2]["text"] == "orphan"
    # A call left unanswered keeps `result: None` rather than stealing the
    # next turn's result.
    tail = st.antigravity_entries([(0, json.dumps(rows[0]))])
    assert [e["result"] for e in tail] == [None, None]


def test_antigravity_system_steps_fold_and_contentless_ones_vanish():
    """``SYSTEM``-sourced steps are harness plumbing, folded and labelled;
    ``CONVERSATION_HISTORY`` carries no ``content`` and produces nothing, so
    a resumed session does not open with an empty card."""
    rows = [
        _agy(0, "SYSTEM", "CONVERSATION_HISTORY"),
        _agy(1, "SYSTEM", "EPHEMERAL_MESSAGE", content="<EPHEMERAL_MESSAGE>\nreminder"),
        _agy(2, "SYSTEM", "CHECKPOINT", content="# Resuming from a compaction"),
        _agy(3, "SYSTEM", "ERROR_MESSAGE", status="ERROR",
             error="invalid tool call", exit_code=1),
        _agy(4, "SYSTEM", "SOMETHING_NEW", content="a type this reader has not seen"),
    ]
    entries = st.antigravity_entries([(i * 10, json.dumps(r)) for i, r in enumerate(rows)])
    assert _kinds(entries) == ["system"] * 4
    assert [e["label"] for e in entries] == [
        "ephemeral", "compaction", "error", "system",
    ]
    # An ERROR_MESSAGE with no `content` still shows its `error`.
    assert entries[2]["text"] == "invalid tool call"


def test_antigravity_flat_file_truncation_and_double_encoded_args():
    """The fallback ``transcript.jsonl`` cuts long fields itself (naming
    them in ``truncated_fields``) and JSON-encodes each tool argument a
    second time. Both are undone here, or a turn would report itself whole
    when it is not and a tool card would show escaped quotes."""
    rows = [
        _agy(0, "MODEL", "PLANNER_RESPONSE", content="a short reply",
             thinking="a cut thought", truncated_fields=["content", "thinking"]),
        _agy(1, "MODEL", "PLANNER_RESPONSE", tool_calls=[{
            "name": "run_command",
            "args": {"CommandLine": "\"dir C:\\\\tmp\"", "toolSummary": "\"Listing\""},
        }]),
    ]
    entries = st.antigravity_entries([(i * 10, json.dumps(r)) for i, r in enumerate(rows)])
    assert [e["kind"] for e in entries] == ["thinking", "assistant", "tool_call"]
    assert entries[0]["truncated"] is True and entries[1]["truncated"] is True
    assert entries[2]["summary"] == "dir C:\\tmp"
    # A call whose only string arguments are UI labels falls back to them.
    labels_only = st.antigravity_entries([(0, json.dumps(_agy(
        0, "MODEL", "PLANNER_RESPONSE",
        tool_calls=[{"name": "wait", "args": {"Ms": 500, "toolSummary": "Waiting"}}],
    )))])
    assert labels_only[0]["summary"] == "Waiting"


def _agy_conversation(turns: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for i in range(turns):
        rows.append(_agy_prompt(f"prompt {i} " + "p" * i, step=i * 3))
        rows.append(_agy(i * 3 + 1, "MODEL", "PLANNER_RESPONSE", thinking=f"think {i}",
                         tool_calls=[{"name": "list_dir", "args": {"DirectoryPath": f"d{i}"}}]))
        rows.append(_agy(i * 3 + 2, "MODEL", "GENERIC",
                         content=f"Created At: t\nCompleted At: t\nout {i}"))
        rows.append(_agy(i * 3 + 3, "MODEL", "PLANNER_RESPONSE", content=f"answer {i}"))
    return rows


def test_antigravity_pages_concatenate_across_window_boundaries(tmp_path: Path, monkeypatch):
    """Every page, oldest-first, equals one whole parse — nothing split or
    duplicated when the window edge lands mid-turn."""
    monkeypatch.setattr(st, "WINDOW_BYTES", 400)
    path = _write_jsonl(tmp_path / "agy.jsonl", _agy_conversation(20))
    pages = _walk(path, 4, flavor="antigravity")
    assert len(pages) > 1
    assert [e for page in reversed(pages) for e in page] == _parse_whole(
        path, st.antigravity_entries
    )


def test_antigravity_entry_full_text_is_uncapped(tmp_path: Path, monkeypatch):
    """A truncated turn reads back whole — what Chat's copy (#985) and
    read-aloud (#988) call when a page entry came back ``truncated``."""
    monkeypatch.setattr(st, "ASSISTANT_TEXT_CAP", 40)
    long_reply = "paragraph one. " * 40
    path = _write_jsonl(tmp_path / "agy.jsonl", [
        _agy_prompt("ask"),
        _agy(1, "MODEL", "PLANNER_RESPONSE", thinking="brief", content=long_reply),
    ])
    page = st.transcript_page(path, flavor="antigravity")
    reply = [e for e in page["entries"] if e["kind"] == "assistant"][0]
    assert reply["truncated"] is True and len(reply["text"]) == 40
    full = st.entry_full_text(path, reply["offset"], "antigravity")
    assert full["text"] == long_reply.strip() and full["truncated"] is False
    assert st.entry_full_text(path, 3, "antigravity") is None       # mid-line
    assert st.entry_full_text(path, 99_999, "antigravity") is None  # past EOF


# ----------------------------------------- antigravity source correlation


def _agy_tree(root: Path, folder: str, uuid: str, *, full: bool = True) -> Path:
    """An Antigravity home with one conversation logged for ``folder``."""
    (root / "cache").mkdir(parents=True, exist_ok=True)
    (root / "cache" / "last_conversations.json").write_text(
        json.dumps({folder: uuid}), encoding="utf-8"
    )
    logs = root / "brain" / uuid / ".system_generated" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    written = logs / ("transcript_full.jsonl" if full else "transcript.jsonl")
    written.write_text("{}\n", encoding="utf-8")
    return written


def _agy_session(**extra: Any) -> Dict[str, Any]:
    row = {"session_id": "s1", "agent": "antigravity", "alive": True,
           "project_dir": r"E:\work\project", "started_at": 1_000_000.0}
    row.update(extra)
    return row


def test_find_antigravity_transcript_uses_the_newest_conversation_cache(
    tmp_path: Path, monkeypatch
):
    """Antigravity links its conversation to nothing the launcher knows, and
    omits `conversationId` on a conversation's first history row — so the
    correlation is its own ``workspace → newest conversation`` cache, which
    *is* written the moment the first prompt lands (#1014 probe)."""
    from src import board_exchange

    uuid = "595bd0ae-ec7f-41dd-a242-2fe7517ee288"
    wanted = _agy_tree(tmp_path, r"E:\work\project", uuid)
    monkeypatch.setattr(board_exchange, "_AGY_ROOT", tmp_path)
    session = _agy_session()
    assert board_exchange.find_antigravity_transcript(session, [session]) == wanted
    # The path separator and case of the cache key need not match the
    # session's own spelling.
    assert board_exchange.find_antigravity_transcript(
        _agy_session(project_dir="e:/work/project/"), []
    ) == wanted
    # A different folder is not this session's conversation.
    assert board_exchange.find_antigravity_transcript(
        _agy_session(project_dir=r"E:\work\other"), []
    ) is None


def test_find_antigravity_transcript_falls_back_to_the_flat_file(tmp_path: Path, monkeypatch):
    """``transcript_full.jsonl`` is preferred — it alone can serve
    ``entry_full_text`` uncapped — but 55 of 265 conversations on this box
    have only the flat file, so its absence is a fallback, not an error."""
    from src import board_exchange

    uuid = "595bd0ae-ec7f-41dd-a242-2fe7517ee288"
    flat = _agy_tree(tmp_path, r"E:\work\project", uuid, full=False)
    monkeypatch.setattr(board_exchange, "_AGY_ROOT", tmp_path)
    assert board_exchange.find_antigravity_transcript(_agy_session(), []) == flat
    full = flat.with_name("transcript_full.jsonl")
    full.write_text("{}\n", encoding="utf-8")
    assert board_exchange.find_antigravity_transcript(_agy_session(), []) == full


def test_find_antigravity_transcript_fails_safe_on_ambiguity_and_staleness(
    tmp_path: Path, monkeypatch
):
    """The cache answers "the newest conversation in this folder", not "this
    session's" — so two guards keep it from showing another session's text,
    the rule ``_find_codex_transcript`` follows.
    """
    from src import board_exchange

    uuid = "595bd0ae-ec7f-41dd-a242-2fe7517ee288"
    wanted = _agy_tree(tmp_path, r"E:\work\project", uuid)
    monkeypatch.setattr(board_exchange, "_AGY_ROOT", tmp_path)

    # A second live Antigravity session in the same folder: the cache cannot
    # say which of the two the uuid belongs to.
    sibling = _agy_session(session_id="s2")
    assert board_exchange.find_antigravity_transcript(_agy_session(), [sibling]) is None
    # Not a sibling: a dead one, another agent, another folder.
    for other in (
        _agy_session(session_id="s2", alive=False),
        _agy_session(session_id="s2", agent="claude"),
        _agy_session(session_id="s2", project_dir=r"E:\work\other"),
    ):
        assert board_exchange.find_antigravity_transcript(_agy_session(), [other]) == wanted

    # Nothing written since this session started: the cache is pointing at
    # whatever ran in the folder before it, and there is no transcript yet.
    import os

    stamp = 1_000_000.0 - board_exchange._AGY_MTIME_SLOP_SECONDS - 1
    os.utime(wanted, (stamp, stamp))
    assert board_exchange.find_antigravity_transcript(_agy_session(), []) is None

    # A session with no usable launch time, and a cache value that is not a
    # conversation uuid, are both refused before any path is built.
    os.utime(wanted, (2_000_000.0, 2_000_000.0))
    assert board_exchange.find_antigravity_transcript(
        _agy_session(started_at="not a time"), []
    ) is None
    (tmp_path / "cache" / "last_conversations.json").write_text(
        json.dumps({r"E:\work\project": "../../../etc"}), encoding="utf-8"
    )
    assert board_exchange.find_antigravity_transcript(_agy_session(), []) is None


# --------------------------------------------------- pi source correlation


def test_find_pi_transcript_matches_the_state_row_key(tmp_path: Path, monkeypatch):
    """Pi's state row is keyed by Pi's own session uuid and carries no
    ``transcript_path``; the file is named after that same uuid (#1013)."""
    from src import board_exchange

    sid = "01a0b0e2-84d4-787a-ae98-eef7f26179bc"
    folder = tmp_path / "--E--work-project--"
    folder.mkdir()
    wanted = folder / f"2026-09-17T19-40-30-292Z_{sid}.jsonl"
    wanted.write_text("{}\n", encoding="utf-8")
    (folder / "2026-09-06T12-14-59-246Z_01a076a4-aeae-789e-80fb-39494a6844c9.jsonl").write_text(
        "{}\n", encoding="utf-8"
    )
    monkeypatch.setattr(board_exchange, "_PI_SESSIONS_DIR", tmp_path)
    assert board_exchange.find_pi_transcript(sid) == wanted
    assert board_exchange.find_pi_transcript("01a076a4-0000-0000-0000-000000000000") is None


def test_find_pi_transcript_refuses_ambiguity_and_glob_syntax(tmp_path: Path, monkeypatch):
    """Fail safe, the rule ``_find_codex_transcript`` follows: anything but
    exactly one match answers ``None`` so the route says "no transcript"
    rather than risking a neighbouring session's text. A key carrying glob
    metacharacters is refused before it can match anything at all."""
    from src import board_exchange

    sid = "01a0b0e2-84d4-787a-ae98-eef7f26179bc"
    for name in ("--E--work-a--", "--E--work-b--"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / f"2026-09-17T19-40-30-292Z_{sid}.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(board_exchange, "_PI_SESSIONS_DIR", tmp_path)
    assert board_exchange.find_pi_transcript(sid) is None      # two candidates
    assert board_exchange.find_pi_transcript("*") is None      # would match every file
    assert board_exchange.find_pi_transcript("") is None
    assert board_exchange.find_pi_transcript("../../etc") is None


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



# ------------------------------------------------------- copilot_entries

COPILOT_CAPTURE = Path(__file__).parent / "fixtures" / "copilot_events.jsonl"


def _cop(kind: str, data: Dict[str, Any], ts: str = "2026-09-17T21:58:00.000Z",
         **extra: Any) -> Dict[str, Any]:
    row = {"type": kind, "data": data, "id": "e1", "parentId": None, "timestamp": ts}
    row.update(extra)
    return row


def test_copilot_entries_grammar_from_capture():
    """The real capture, parsed whole: the grammar and the turn order.

    Pins what the issue could not know without a capture — the tool records
    are their own ``tool.execution_*`` event pair carrying an exact
    ``toolCallId``, not the ``toolRequests`` array the issue pointed at, and
    a tool-calling ``assistant.message`` has no text of its own.
    """
    entries = _parse_whole(COPILOT_CAPTURE, st.copilot_entries)
    assert _kinds(entries) == [
        "system",                                     # the refused --model flag
        "user", "tool_call", "assistant",
        "user", "thinking", "tool_call", "assistant",
        "user", "thinking", "assistant",
        "user", "tool_call", "assistant",         # the failing view (#1020)
    ]
    # The launcher's model flag is rejected at startup (#1017) — folded, not
    # dropped, so the reason is visible rather than mysterious.
    assert entries[0]["label"] == "model"
    assert "is not available" in entries[0]["text"]
    assert entries[1]["text"].startswith("Run the shell command: git rev-parse")
    # The call: name and arguments come from `tool.execution_start`, and the
    # result is paired back by its exact id.
    assert entries[2]["name"] == "powershell"
    assert entries[2]["summary"] == "git rev-parse --short HEAD"
    assert entries[2]["result"].startswith("d9580a3")
    assert entries[3]["text"] == "d9580a3"
    # A tool call always precedes the reply that reports it: the call is
    # written on a later line than the assistant message that requested it,
    # so no answer is ever hoisted above its own call (#1012's trap). Read
    # from the raw builder — `_page` strips `offset` off the folded kinds.
    raw = st.copilot_entries(_lines_of(COPILOT_CAPTURE))
    call, reply = raw[2], raw[3]
    assert call["kind"] == "tool_call" and reply["kind"] == "assistant"
    assert call["offset"] < reply["offset"]
    # Copilot's two outcome fields, at two levels — both needed (#1020).
    # A command that exits 1 is still `success: true` (the *tool* ran fine),
    # so the failure is read from `shellExecution.exitCode`. The exit-code
    # footer stays in the card text too.
    assert entries[6]["summary"] == "git rev-parse --short NO_SUCH_REF_xyz"
    assert "exit code 1" in entries[6]["result"]
    assert entries[6]["error"] is True
    assert "error" not in entries[2]           # exitCode 0, same capture
    assert entries[7]["text"] == "fatal: Needed a single revision"
    assert entries[10]["text"].count("\n\n") == 2       # the multi-paragraph reply
    # A *tool*-level failure is the other shape: `success: false` with an
    # `error` object and **no `result` key at all** — so the card's text has
    # to come from `error.message`, or a failed call renders blank.
    assert entries[12]["name"] == "view"
    assert entries[12]["error"] is True
    assert entries[12]["result"] == "Path does not exist"


def test_copilot_plumbing_never_reaches_a_card():
    """The three families that would wreck the view if a reader guessed.

    Each marker is a real record in the capture, so this fails the moment
    the parser starts matching loosely rather than on exact event types.
    """
    entries = _parse_whole(COPILOT_CAPTURE, st.copilot_entries)
    blob = json.dumps(entries)
    # 1. The system prompt: one ~87 KB line in the real log.
    assert "SYSTEM_PROMPT_MUST_NEVER_RENDER" not in blob
    # 2. `model.*` bookkeeping re-serialises the whole conversation on every
    #    model call — rendering it would multiply every turn.
    assert "DUPLICATE_MUST_NEVER_RENDER" not in blob
    # 3. `encryptedContent` / `reasoningOpaque` / `reasoningBlocks` are
    #    provider blobs; a card must never show a screenful of base64.
    assert "OPAQUE_MUST_NEVER_RENDER" not in blob
    # Exactly one tool card per call — `toolRequests` on the assistant
    # message is the same call again and must not double it.
    assert _kinds(entries).count("tool_call") == 3


def test_copilot_user_prompt_is_the_typed_text_not_the_wrapped_one(tmp_path: Path):
    """``content`` is what the user typed; ``transformedContent`` is the same
    text wrapped in a ``<current_datetime>`` block for the model."""
    path = _write_jsonl(tmp_path / "cop.jsonl", [
        _cop("user.message", {
            "content": "say hi",
            "transformedContent": "<current_datetime>2026-09-17T19:24:03+02:00"
                                  "</current_datetime>\n\nsay hi",
        }),
    ])
    entries = _parse_whole(path, st.copilot_entries)
    assert [e["text"] for e in entries] == ["say hi"]


def test_copilot_tool_results_pair_by_id_and_orphans_stand_alone(tmp_path: Path):
    """``toolCallId`` is exact, so interleaved calls pair correctly and a
    result whose call fell off the page stands alone rather than mis-pairing.
    """
    path = _write_jsonl(tmp_path / "cop.jsonl", [
        _cop("tool.execution_start", {"toolCallId": "a", "toolName": "view",
                                      "arguments": {"path": "one.txt"}}),
        _cop("tool.execution_start", {"toolCallId": "b", "toolName": "view",
                                      "arguments": {"path": "two.txt"}}),
        # Answered out of order: the ids, not the order, decide the pairing.
        _cop("tool.execution_complete", {"toolCallId": "b", "success": True,
                                         "result": {"content": "two body"}}),
        _cop("tool.execution_complete", {"toolCallId": "a", "success": True,
                                         "result": {"content": "one body"}}),
        _cop("tool.execution_complete", {"toolCallId": "gone", "success": True,
                                         "result": {"content": "orphan body"}}),
    ])
    entries = _parse_whole(path, st.copilot_entries)
    assert _kinds(entries) == ["tool_call", "tool_call", "tool_result"]
    assert entries[0]["summary"] == "one.txt" and entries[0]["result"] == "one body"
    assert entries[1]["summary"] == "two.txt" and entries[1]["result"] == "two body"
    assert entries[2]["text"] == "orphan body" and entries[2]["tool_use_id"] == "gone"


def test_copilot_result_prefers_content_over_detailed_and_placeholders_binary(
    tmp_path: Path,
):
    """``content`` and ``detailedContent`` are different views, not a
    short/long pair: on the three completions here where they differ,
    ``content`` was the tool output the model saw. A non-textual result
    becomes a placeholder instead of a dump."""
    path = _write_jsonl(tmp_path / "cop.jsonl", [
        _cop("tool.execution_start", {"toolCallId": "a", "toolName": "view",
                                      "arguments": {"path": "f"}}),
        _cop("tool.execution_complete", {"toolCallId": "a", "success": True,
                                         "result": {"content": "the file body",
                                                    "detailedContent": "a diff of it"}}),
        _cop("tool.execution_start", {"toolCallId": "b", "toolName": "view",
                                      "arguments": {"path": "g"}}),
        _cop("tool.execution_complete", {"toolCallId": "b", "success": True,
                                         "result": {"detailedContent": "only detailed"}}),
        _cop("tool.execution_start", {"toolCallId": "c", "toolName": "screenshot",
                                      "arguments": {"path": "h"}}),
        _cop("tool.execution_complete", {"toolCallId": "c", "success": True,
                                         "result": {"image": {"bytes": 1}}}),
    ])
    entries = _parse_whole(path, st.copilot_entries)
    assert entries[0]["result"] == "the file body"
    assert entries[1]["result"] == "only detailed"          # fallback
    assert entries[2]["result"] == "[non-text result]"      # never a repr dump


def test_copilot_readable_thinking_folds_and_opaque_reasoning_does_not(tmp_path: Path):
    """``reasoningText`` is prose and folds as ``thinking``; the three
    encrypted neighbours are never read at all."""
    path = _write_jsonl(tmp_path / "cop.jsonl", [
        _cop("assistant.message", {
            "content": "the answer",
            "reasoningText": "  weighing the options  ",
            "reasoningOpaque": "AAAABBBBCCCC",
            "encryptedContent": "DDDDEEEEFFFF",
            "reasoningBlocks": {"blocks": [{"encrypted_content": "GGGGHHHH"}]},
            "apiCallId": "IIIIJJJJ",
        }),
    ])
    entries = _parse_whole(path, st.copilot_entries)
    assert _kinds(entries) == ["thinking", "assistant"]
    assert entries[0]["text"] == "weighing the options"
    blob = json.dumps(entries)
    for opaque in ("AAAABBBB", "DDDDEEEE", "GGGGHHHH", "IIIIJJJJ"):
        assert opaque not in blob


def test_copilot_timestamps_are_the_envelope_iso_string(tmp_path: Path):
    """ISO-8601 on the event envelope, passed through untouched — the
    client's ``new Date`` reads it, so no turn renders as 1970 (#1012)."""
    path = _write_jsonl(tmp_path / "cop.jsonl", [
        _cop("user.message", {"content": "hi"}, ts="2026-09-17T21:58:01.005Z"),
    ])
    entries = _parse_whole(path, st.copilot_entries)
    assert entries[0]["timestamp"] == "2026-09-17T21:58:01.005Z"


def _copilot_conversation(turns: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for i in range(turns):
        rows.append(_cop("user.message", {"content": f"prompt {i} " + "p" * i}))
        # The real log's dominant weight: bookkeeping between every turn.
        rows.append(_cop("model.messages_snapshot",
                         {"messages": [{"role": "user", "content": "noise " * 8}]}))
        rows.append(_cop("assistant.message",
                         {"content": "", "toolRequests": [{"toolCallId": f"c{i}"}]}))
        rows.append(_cop("tool.execution_start", {"toolCallId": f"c{i}",
                                                  "toolName": "powershell",
                                                  "arguments": {"command": f"cmd {i}"}}))
        rows.append(_cop("tool.execution_complete", {"toolCallId": f"c{i}",
                                                     "success": True,
                                                     "result": {"content": f"out {i}"}}))
        rows.append(_cop("assistant.message", {"content": f"answer {i}",
                                               "phase": "final_answer"}))
    return rows


def test_copilot_pages_concatenate_across_window_boundaries(tmp_path: Path, monkeypatch):
    """Every page, oldest-first, equals one whole parse — nothing split or
    duplicated when the window edge lands mid-turn."""
    monkeypatch.setattr(st, "WINDOW_BYTES", 400)
    path = _write_jsonl(tmp_path / "cop.jsonl", _copilot_conversation(20))
    pages = _walk(path, 4, flavor="copilot")
    assert len(pages) > 1
    assert [e for page in reversed(pages) for e in page] == _parse_whole(
        path, st.copilot_entries
    )


def test_copilot_entry_full_text_is_uncapped(tmp_path: Path, monkeypatch):
    """A truncated turn reads back whole — what Chat's copy (#985) and
    read-aloud (#988) call when a page entry came back ``truncated``."""
    monkeypatch.setattr(st, "ASSISTANT_TEXT_CAP", 40)
    long_reply = "paragraph one. " * 40
    path = _write_jsonl(tmp_path / "cop.jsonl", [
        _cop("user.message", {"content": "ask"}),
        _cop("assistant.message", {"content": long_reply, "reasoningText": "brief"}),
    ])
    page = st.transcript_page(path, flavor="copilot")
    reply = [e for e in page["entries"] if e["kind"] == "assistant"][0]
    assert reply["truncated"] is True and len(reply["text"]) == 40
    full = st.entry_full_text(path, reply["offset"], "copilot")
    assert full["text"] == long_reply.strip() and full["truncated"] is False
    assert st.entry_full_text(path, 3, "copilot") is None       # mid-line
    assert st.entry_full_text(path, 99_999, "copilot") is None  # past EOF


def test_copilot_huge_plumbing_line_does_not_break_paging(tmp_path: Path, monkeypatch):
    """A single ``system.message`` reached 87 KB in the capture — wider than
    a window once ``WINDOW_BYTES`` is small. The page widens in place rather
    than tearing it, and the turns around it still read whole."""
    monkeypatch.setattr(st, "WINDOW_BYTES", 400)
    path = _write_jsonl(tmp_path / "cop.jsonl", [
        _cop("user.message", {"content": "ask"}),
        _cop("system.message", {"role": "system", "content": "S" * 5_000}),
        _cop("assistant.message", {"content": "answer", "phase": "final_answer"}),
    ])
    page = st.transcript_page(path, flavor="copilot")
    assert _kinds(page["entries"]) == ["user", "assistant"]
    assert page["entries"][1]["text"] == "answer"


# -------------------------------------------- copilot source correlation


def _copilot_session_dir(root: Path, folder: str, cwd: str, created: str,
                       *, events: bool = True) -> Path:
    """One Copilot session folder, as the harness lays it out at launch."""
    session = root / folder
    session.mkdir(parents=True, exist_ok=True)
    (session / "workspace.yaml").write_text(
        "id: {0}\ncwd: {1}\ngit_root: {1}\nclient_name: github/cli\n"
        "created_at: {2}\nupdated_at: {2}\n".format(folder, cwd, created),
        encoding="utf-8",
    )
    events_path = session / "events.jsonl"
    if events:
        events_path.write_text("{}\n", encoding="utf-8")
    return events_path


def _copilot_session(**extra: Any) -> Dict[str, Any]:
    # 2026-09-17T21:57:30.465Z, the capture's own launch instant.
    row = {"session_id": "s1", "agent": "copilot", "alive": True,
           "project_dir": r"E:\work\project", "started_at": 1_789_682_250.465}
    row.update(extra)
    return row


def test_find_copilot_transcript_matches_cwd_and_launch_window(tmp_path: Path, monkeypatch):
    """Copilot writes the launcher nothing — no sessions-state row, and its
    own log never contains the launcher's session id — so the correlation is
    its ``workspace.yaml`` sidecar: this folder's ``cwd``, created inside the
    launch window. Measured on two real launches: 3.71 s and 3.72 s after
    ``started_at`` (#1015 capture)."""
    from src import board_exchange

    wanted = _copilot_session_dir(
        tmp_path, "79c18133-52bd-4f8f-9260-b33c853158be",
        r"E:\work\project", "2026-09-17T21:57:34.176Z",
    )
    monkeypatch.setattr(board_exchange, "_COPILOT_STATE_DIR", tmp_path)
    assert board_exchange.find_copilot_transcript(_copilot_session()) == wanted
    # Neither the separator nor the case of the harness's own spelling has
    # to match the session's.
    assert board_exchange.find_copilot_transcript(
        _copilot_session(project_dir="e:/work/project/")
    ) == wanted
    # A different folder is not this session's conversation.
    assert board_exchange.find_copilot_transcript(
        _copilot_session(project_dir=r"E:\work\other")
    ) is None
    # A session launched long before this folder existed is not its owner.
    assert board_exchange.find_copilot_transcript(
        _copilot_session(started_at=1_789_600_000.0)
    ) is None
    # Nor one launched *after* it: the folder is created at launch, so a
    # later session's folder is a different one.
    assert board_exchange.find_copilot_transcript(
        _copilot_session(started_at=1_789_682_400.0)
    ) is None


def test_find_copilot_transcript_refuses_two_candidates_in_one_window(
    tmp_path: Path, monkeypatch
):
    """Two sessions started in one folder inside the window are ambiguous by
    construction — the sidecar says which folder, never which session — so
    both refuse rather than risk showing the other's conversation, the rule
    ``_find_codex_transcript`` follows."""
    from src import board_exchange

    _copilot_session_dir(tmp_path, "aaaaaaaa-0000-0000-0000-000000000001",
                       r"E:\work\project", "2026-09-17T21:57:34.176Z")
    monkeypatch.setattr(board_exchange, "_COPILOT_STATE_DIR", tmp_path)
    assert board_exchange.find_copilot_transcript(_copilot_session()) is not None
    # A second launch in the same folder, 20 s later — still inside the
    # window, so neither folder can be claimed.
    _copilot_session_dir(tmp_path, "aaaaaaaa-0000-0000-0000-000000000002",
                       r"E:\work\project", "2026-09-17T21:57:54.176Z")
    assert board_exchange.find_copilot_transcript(_copilot_session()) is None
    # A same-folder session outside the window does not make the first
    # ambiguous — which is what keeps the ordinary case answerable.
    for stale in tmp_path.glob("aaaaaaaa-0000-0000-0000-000000000002/*"):
        stale.unlink()
    _copilot_session_dir(tmp_path, "aaaaaaaa-0000-0000-0000-000000000003",
                       r"E:\work\project", "2026-09-17T22:05:00.000Z")
    assert board_exchange.find_copilot_transcript(_copilot_session()) is not None


def test_find_copilot_transcript_needs_an_events_file(tmp_path: Path, monkeypatch):
    """The folder and its sidecar are written at launch, ``events.jsonl``
    only at the first prompt — so a session nobody has typed into has no
    transcript, and neither do the 28 of 71 older sessions here that kept a
    ``session.db`` instead."""
    from src import board_exchange

    monkeypatch.setattr(board_exchange, "_COPILOT_STATE_DIR", tmp_path)
    events = _copilot_session_dir(
        tmp_path, "79c18133-52bd-4f8f-9260-b33c853158be",
        r"E:\work\project", "2026-09-17T21:57:34.176Z", events=False,
    )
    assert board_exchange.find_copilot_transcript(_copilot_session()) is None
    events.write_text("{}\n", encoding="utf-8")
    assert board_exchange.find_copilot_transcript(_copilot_session()) == events


def test_find_copilot_transcript_survives_a_damaged_sidecar(tmp_path: Path, monkeypatch):
    """A folder whose ``workspace.yaml`` is missing, unparseable or lacks the
    two keys matches nothing — it must not raise, and must not swallow the
    real match sitting beside it."""
    from src import board_exchange

    monkeypatch.setattr(board_exchange, "_COPILOT_STATE_DIR", tmp_path)
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "workspace.yaml").write_text(
        "cwd: E:\\work\\project\ncreated_at: not-a-timestamp\n", encoding="utf-8"
    )
    (tmp_path / "keyless").mkdir()
    (tmp_path / "keyless" / "workspace.yaml").write_text("id: x\n", encoding="utf-8")
    assert board_exchange.find_copilot_transcript(_copilot_session()) is None
    wanted = _copilot_session_dir(
        tmp_path, "79c18133-52bd-4f8f-9260-b33c853158be",
        r"E:\work\project", "2026-09-17T21:57:34.176Z",
    )
    assert board_exchange.find_copilot_transcript(_copilot_session()) == wanted


def test_find_copilot_transcript_needs_a_dir_and_a_start(tmp_path: Path, monkeypatch):
    """A session row missing either half of the correlation answers None
    rather than scanning for a best guess."""
    from src import board_exchange

    monkeypatch.setattr(board_exchange, "_COPILOT_STATE_DIR", tmp_path)
    _copilot_session_dir(tmp_path, "79c18133-52bd-4f8f-9260-b33c853158be",
                       r"E:\work\project", "2026-09-17T21:57:34.176Z")
    assert board_exchange.find_copilot_transcript(_copilot_session(project_dir="")) is None
    assert board_exchange.find_copilot_transcript(_copilot_session(started_at=None)) is None
    # An ISO `started_at` (a row read back from JSON state) works too.
    assert board_exchange.find_copilot_transcript(
        _copilot_session(started_at="2026-09-17T21:57:30.465Z")
    ) is not None


# -------------------------------------------- claude source correlation


def _claude_folder(root: Path, name: str, *files: Tuple[str, float]) -> Path:
    """One ``~/.claude/projects`` folder with conversations at fixed mtimes."""
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    for stem, written in files:
        path = folder / (stem + ".jsonl")
        path.write_text("{}\n", encoding="utf-8")
        os.utime(path, (written, written))
    return folder


def _claude_session(**extra: Any) -> Dict[str, Any]:
    row = {"session_id": "s1", "agent": "claude", "alive": True,
           "project_dir": r"E:\work\project", "started_at": 1_000_000.0}
    row.update(extra)
    return row


def test_find_claude_transcript_takes_the_newest_in_the_cwd_folder(
    tmp_path: Path, monkeypatch
):
    """The fallback for a live session the hook left no row for (#1023):
    Claude files a conversation under the cwd with every non-alphanumeric
    character replaced by `-`, and the newest one there is this session's."""
    from src import board_exchange

    monkeypatch.setattr(board_exchange, "_CLAUDE_PROJECTS_DIR", tmp_path)
    folder = _claude_folder(
        tmp_path, "E--work-project", ("old", 1_000_100.0), ("new", 1_000_200.0)
    )
    session = _claude_session()
    assert board_exchange.find_claude_transcript(session, [session]) == folder / "new.jsonl"
    # The session's own spelling of the directory need not match the folder's.
    assert board_exchange.find_claude_transcript(
        _claude_session(project_dir="e:/work/project/"), []
    ) == folder / "new.jsonl"
    # A different working directory is a different session's conversation.
    assert board_exchange.find_claude_transcript(
        _claude_session(project_dir=r"E:\work\other"), []
    ) is None


def test_find_claude_transcript_matches_a_lowercased_legacy_folder(
    tmp_path: Path, monkeypatch
):
    """Older Claude Code versions lowercased the folder name as well as
    replacing the separators — 4 of the 107 folders on this box that record
    their own cwd are spelled that way, so the lookup is case-insensitive
    rather than one constructed name."""
    from src import board_exchange

    monkeypatch.setattr(board_exchange, "_CLAUDE_PROJECTS_DIR", tmp_path)
    folder = _claude_folder(tmp_path, "e--work-project", ("conv", 1_000_100.0))
    assert board_exchange.find_claude_transcript(
        _claude_session(), []
    ) == folder / "conv.jsonl"


def test_find_claude_transcript_fails_safe_on_ambiguity_and_staleness(
    tmp_path: Path, monkeypatch
):
    """Two guards, both answering None rather than a confident wrong file:
    a second live Claude session in the same folder makes "newest here"
    meaningless (#537), and a folder untouched since this session started
    holds only what ran there before it."""
    from src import board_exchange

    monkeypatch.setattr(board_exchange, "_CLAUDE_PROJECTS_DIR", tmp_path)
    _claude_folder(tmp_path, "E--work-project", ("conv", 1_000_100.0))
    sibling = _claude_session(session_id="s2")
    assert board_exchange.find_claude_transcript(_claude_session(), [sibling]) is None
    # A dead sibling, one in another folder, or another agent's, all leave
    # exactly one live claimant here.
    for other in (
        _claude_session(session_id="s2", alive=False),
        _claude_session(session_id="s2", project_dir=r"E:\work\other"),
        _claude_session(session_id="s2", agent="codex"),
    ):
        assert board_exchange.find_claude_transcript(_claude_session(), [other]) is not None
    # Nothing written since this session started: refuse, and "no transcript
    # yet" is also the honest answer.
    assert board_exchange.find_claude_transcript(
        _claude_session(started_at=1_000_200.0), []
    ) is None
    # ...but only outside the slop that covers spawn-to-first-write.
    assert board_exchange.find_claude_transcript(
        _claude_session(started_at=1_000_140.0), []
    ) is not None


def test_find_claude_transcript_keeps_its_two_guard_shape_for_transcript(
    tmp_path: Path, monkeypatch
):
    """#1034 criterion 4. The disproof is opt-in, and ``/transcript`` does
    not opt in.

    ``find_claude_transcript`` is shared verbatim by the Board drawer and
    ``/transcript`` (#1023), so #1034 parameterised it rather than forking
    it. This pins the un-opted-in shape: a PTY title that flatly disagrees
    with the conversation's declared name still resolves, because on that
    route the scan has no competing source to outrank — refusing would
    turn a rough answer into no answer at all, and its ``source``
    vocabulary would have to grow a case it never had.
    """
    from src import board_exchange

    monkeypatch.setattr(board_exchange, "_CLAUDE_PROJECTS_DIR", tmp_path)
    folder = _claude_folder(tmp_path, "E--work-project", ("conv", 1_000_100.0))
    (folder / "conv.jsonl").write_text(
        json.dumps({"type": "ai-title", "aiTitle": "A totally different name"})
        + "\n",
        encoding="utf-8",
    )
    session = _claude_session(live_title="◐ Issue 1034")

    assert board_exchange.find_claude_transcript(
        session, [session]
    ) == folder / "conv.jsonl"

    # The same inputs through the drawer's opted-in view do refuse.
    path, verdict = board_exchange._scan_claude_transcript(
        session, [session], disprove=True
    )
    assert path is None
    assert verdict == "disproved"


def test_find_claude_transcript_needs_a_dir_a_start_and_a_folder(
    tmp_path: Path, monkeypatch
):
    """Half a correlation, an empty folder or no projects directory at all
    answer None rather than scanning for a best guess."""
    from src import board_exchange

    monkeypatch.setattr(board_exchange, "_CLAUDE_PROJECTS_DIR", tmp_path)
    _claude_folder(tmp_path, "E--work-project", ("conv", 1_000_100.0))
    assert board_exchange.find_claude_transcript(_claude_session(project_dir=""), []) is None
    assert board_exchange.find_claude_transcript(_claude_session(started_at=None), []) is None
    # An ISO `started_at` (a row read back from JSON state) works too.
    assert board_exchange.find_claude_transcript(
        _claude_session(started_at="1970-01-12T13:46:40Z"), []
    ) is not None
    _claude_folder(tmp_path, "E--work-empty")
    assert board_exchange.find_claude_transcript(
        _claude_session(project_dir=r"E:\work\empty"), []
    ) is None
    monkeypatch.setattr(board_exchange, "_CLAUDE_PROJECTS_DIR", tmp_path / "gone")
    assert board_exchange.find_claude_transcript(_claude_session(), []) is None


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

    def test_live_claude_session_with_no_state_row_still_reads_its_history(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path
    ):
        """#1023 regression: the hook deletes a session's row on `/resume`
        and `/clear` and only the next prompt writes it back, so a live
        session is rowless for as long as its user reads rather than types.
        Before the filesystem fallback that answered `no_transcript` over a
        conversation sitting readable on disk, while Terminal mode showed
        it. The row stays first when there is one."""
        from src import board_exchange

        client, _, overrides = webapp_client
        session = _live(project_dir=r"E:\work\project", started_at=1_000_000.0)
        overrides["session"].list_sessions.return_value = [session]
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: None)
        monkeypatch.setattr(board_exchange, "_CLAUDE_PROJECTS_DIR", tmp_path)
        folder = tmp_path / "E--work-project"
        folder.mkdir()
        path = _write_jsonl(folder / "conv.jsonl", _conversation(2))
        os.utime(path, (1_000_100.0, 1_000_100.0))

        body = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert body["available"] is True and body["source"] == "native"
        assert "prompt 1 p" in [e["text"] for e in body["entries"] if e["kind"] == "user"]
        # A second live session in the same folder makes "newest here"
        # meaningless — refuse rather than show the wrong conversation.
        overrides["session"].list_sessions.return_value = [
            session, _live(sid="s2", project_dir=r"E:\work\project"),
        ]
        refused = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert refused["available"] is False and refused["reason"] == "no_transcript"

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

    def test_grok_row_reads_its_updates_stream(self, webapp_client, _bypass_gate, monkeypatch):
        """A Grok session resolves through the state row's `transcript_path`
        — the same non-Codex path Claude uses — and reports its own source
        name rather than being mislabelled as the Codex reader."""
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(agent="grok")]
        monkeypatch.setattr(
            board, "state_row_for_session",
            lambda live, rows, sid: {"transcript_path": str(GROK_CAPTURE)},
        )
        body = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert body["available"] is True and body["source"] == "grok"
        assert body["tool_errors"] == "reported"
        assert [e["kind"] for e in body["entries"]][:3] == ["user", "thinking", "assistant"]
        # The first exchange's answer — the capture's last assistant belongs
        # to #1020's failing-read exchange, which is one short line.
        entry = [e for e in body["entries"] if e["kind"] == "assistant"][1]
        full = client.get(
            f"/api/claude-code/sessions/s1/transcript/entry?offset={entry['offset']}"
        ).json()
        assert full["available"] is True
        assert full["text"].startswith("The `docs` folder")

    def test_grok_unclaimed_row_is_no_transcript_not_a_stale_neighbour(
        self, webapp_client, _bypass_gate, monkeypatch
    ):
        """Correlation fails safe (#1012): Grok keeps a session folder per
        working directory, including ones that no longer exist (a removed
        worktree). When the claim walk assigns this session no row, the
        answer is `no_transcript` — never a nearby folder's stream, which
        would show another session's text.
        """
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(agent="grok")]
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: None)
        body = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert body["available"] is False and body["reason"] == "no_transcript"
        assert body["entries"] == []

    def test_copilot_row_reads_its_event_log(
        self, webapp_client, _bypass_gate, monkeypatch
    ):
        """A Copilot session has no state row either — its hooks do not fire
        in the interactive TUI — so the route correlates its event log from
        the filesystem and reports its own source name rather than falling
        through to Codex's."""
        from app.webapp.routers import session_transcript as router_mod
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(agent="copilot")]
        monkeypatch.setattr(
            router_mod, "find_copilot_transcript", lambda session: COPILOT_CAPTURE,
        )
        body = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert body["available"] is True and body["source"] == "copilot"
        # Copilot cannot mark every failure, and the page says so (#1020).
        assert body["tool_errors"] == "partial"
        assert [e["kind"] for e in body["entries"]][:3] == ["system", "user", "tool_call"]
        # The no-tools reply — the capture's last assistant belongs to
        # #1020's failing-view exchange, which is one short line.
        entry = [e for e in body["entries"] if e["kind"] == "assistant"][2]
        full = client.get(
            f"/api/claude-code/sessions/s1/transcript/entry?offset={entry['offset']}"
        ).json()
        assert full["available"] is True
        assert full["text"].startswith("Bounded file reading keeps log viewers")
        # An ambiguous correlation (two sessions launched in one folder inside
        # the window) is "no transcript", never a neighbour's conversation.
        monkeypatch.setattr(router_mod, "find_copilot_transcript", lambda session: None)
        assert client.get(
            "/api/claude-code/sessions/s1/transcript"
        ).json()["reason"] == "no_transcript"

    def test_antigravity_row_reads_its_conversation_log(
        self, webapp_client, _bypass_gate, monkeypatch
    ):
        """An Antigravity session has no state row at all — the fleet's
        `session_state` hook does not reach it — so the route resolves its
        log from the filesystem and reports its own source name."""
        from app.webapp.routers import session_transcript as router_mod
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(agent="antigravity")]
        monkeypatch.setattr(
            router_mod, "find_antigravity_transcript",
            lambda session, live: AGY_CAPTURE,
        )
        body = client.get("/api/claude-code/sessions/s1/transcript").json()
        assert body["available"] is True and body["source"] == "antigravity"
        assert [e["kind"] for e in body["entries"]][:3] == ["user", "thinking", "tool_call"]
        entry = [e for e in body["entries"] if e["kind"] == "assistant"][0]
        full = client.get(
            f"/api/claude-code/sessions/s1/transcript/entry?offset={entry['offset']}"
        ).json()
        assert full["available"] is True
        assert full["text"].startswith("Based on the directory listing")
        # Correlation refused (a second live session in the folder, or a
        # session that has not prompted yet) is "no transcript", never a
        # neighbour's conversation.
        monkeypatch.setattr(
            router_mod, "find_antigravity_transcript", lambda session, live: None
        )
        assert client.get(
            "/api/claude-code/sessions/s1/transcript"
        ).json()["reason"] == "no_transcript"

    # `ssh` on purpose: it is the one registered agent that is not a coding
    # harness and so will never have a history file. Every coding agent the
    # launcher hosts now has a reader (#1015 was the last), so picking one of
    # them here would mean repointing these two tests again, as #1013 had to
    # when Pi stopped being unsupported.
    def test_detached_unsupported_agent(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(kind="remote", agent="ssh")]
        assert client.get("/api/claude-code/sessions/s1/transcript").json()["reason"] == "unsupported_agent"

    def test_unsupported_agent(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live(agent="ssh")]
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


def test_transcript_entry_path_classified_passkey():
    """#985: a distinct guard-table row — '.../transcript/entry' does not
    end with '/transcript', so the sibling route's rule does not cover it
    for free (the exact omission class #997 found on the send route)."""
    from app.webapp.middleware import _terminal_guard_level
    assert _terminal_guard_level("/api/claude-code/sessions/abc/transcript/entry") == "passkey"
    assert _terminal_guard_level("/api/claude-code/sessions/abc/transcript") == "passkey"


def test_transcript_entry_refused_off_tailnet(webapp_client):
    client, _, _ = webapp_client
    assert client.get("/api/claude-code/sessions/s1/transcript/entry?offset=0").status_code == 403


class TestTranscriptEntryEndpoint:

    def test_offset_is_required(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live()]
        assert client.get("/api/claude-code/sessions/s1/transcript/entry").status_code == 422
        assert client.get("/api/claude-code/sessions/s1/transcript/entry?offset=-1").status_code == 422

    def test_unknown_session_shares_the_page_routes_reasons(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = []
        body = client.get("/api/claude-code/sessions/s1/transcript/entry?offset=0").json()
        assert body["available"] is False and body["reason"] == "session_not_found"

    def test_happy_path_returns_uncapped_text_and_never_logs_body(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path, caplog
    ):
        monkeypatch.setattr(st, "USER_TEXT_CAP", 5)
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live()]
        path = _write_jsonl(tmp_path / "t.jsonl", [_user("a much longer prompt than the cap")])
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: {"transcript_path": str(path)})
        page = client.get("/api/claude-code/sessions/s1/transcript").json()
        entry = page["entries"][0]
        assert entry["truncated"] is True

        with caplog.at_level(logging.INFO):
            body = client.get(
                f"/api/claude-code/sessions/s1/transcript/entry?offset={entry['offset']}"
            ).json()
        assert body == {
            "available": True, "reason": None, "session_id": "s1",
            "text": "a much longer prompt than the cap", "truncated": False,
        }
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "a much longer prompt" not in joined
        assert "transcript entry s1" in joined

    def test_stale_offset_is_entry_not_found(self, webapp_client, _bypass_gate, monkeypatch, tmp_path):
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live()]
        path = _write_jsonl(tmp_path / "t.jsonl", [_user("hi")])
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: {"transcript_path": str(path)})
        body = client.get("/api/claude-code/sessions/s1/transcript/entry?offset=9999").json()
        assert body["available"] is False and body["reason"] == "entry_not_found"

    def test_read_failure_is_distinct_from_entry_not_found(
        self, webapp_client, _bypass_gate, monkeypatch, tmp_path
    ):
        from app.webapp.routers import session_transcript as router_mod
        client, _, overrides = webapp_client
        overrides["session"].list_sessions.return_value = [_live()]
        path = _write_jsonl(tmp_path / "t.jsonl", [_user("hi")])
        monkeypatch.setattr(board, "state_row_for_session", lambda live, rows, sid: {"transcript_path": str(path)})

        def boom(*args, **kwargs):
            raise PermissionError("locked")

        monkeypatch.setattr(router_mod, "entry_full_text", boom)
        body = client.get("/api/claude-code/sessions/s1/transcript/entry?offset=0").json()
        assert body["available"] is False and body["reason"] == "read_failed"


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
