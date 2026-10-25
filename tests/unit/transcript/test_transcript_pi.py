"""Coding-tab transcript: the Pi entry grammar and its paging."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from src.session_view import transcript as st
from src.transcript_flavors import _shared as tf_shared

from tests.unit.transcript._support import _kinds, _lines_of, _parse_whole, _png_b64, _walk, _write_jsonl


# A Pi session captured for #1013 by launching Pi through the launcher's own
# API and driving it over `/input`, so the correlation below is the real one.
# Two turns: a read-only tool call whose result succeeds, and one whose
# result fails (`isError: true`) — plus the `quit`/`Goodbye.` pair the
# launcher's Stop leaks into the history, left in on purpose (the parser must
# not special-case it; it is a stop-path bug tracked separately).
# Redacted: the working directory becomes `E:\work\project`, and the
# `thinkingSignature` / `textSignature` blobs are trimmed to 80 chars.
PI_CAPTURE = Path(__file__).resolve().parents[2] / "fixtures" / "pi_session.jsonl"


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
    assert entries[4]["action"]["verb"] == "ran"
    assert entries[4]["action"]["command"].startswith("find docs")
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


def test_pi_image_tool_result_is_a_thumbnail_ref(tmp_path: Path):
    """An ``image`` block carries raw base64 in ``data`` (35 of them in the
    corpus, from screenshot tools). The page never carries the bytes: the
    result lists where the image is (#1265), and the image route decodes it
    on demand, sniffing its type because Pi records none."""
    blob = _png_b64()
    path = _write_jsonl(tmp_path / "pi.jsonl", [
        _pi_msg("assistant", [{"type": "toolCall", "id": "t1", "name": "screenshot",
                               "arguments": {"path": "shot.png"}}]),
        _pi_msg("toolResult", [_pi_text("captured"), {"type": "image", "data": blob}],
                mid="m2", toolCallId="t1", toolName="screenshot", isError=False),
    ])
    call = _parse_whole(path, st.pi_entries)[0]
    assert call["result"] == "captured"
    assert blob[:12] not in json.dumps(call)
    [ref] = call["result_images"]
    assert ref["n"] == 0
    data, media = st.transcript_image(path, ref["offset"], ref["n"], "pi")
    assert media == "image/png" and data.startswith(b"\x89PNG")


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
    monkeypatch.setattr(tf_shared, "ASSISTANT_TEXT_CAP", 40)
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
