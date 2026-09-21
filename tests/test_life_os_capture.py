"""Capture → transcript-entry parsing (#1119).

Every capture here is synthetic: the real life-os checkout holds private
conversations and is never read by a test.
"""

from __future__ import annotations

from src.life_os_capture import parse_capture

_CLAUDE = """journal-daily #4

<!-- capture sid="9ff0b2f4-0000-4000-8000-000000000001" agent="claude" updated="2026-09-19T09:47:43+00:00" -->

**You**: book the ferry for Friday

**Claude**: Booked the 07:40.

**Recommended fix**: confirm the return leg.

**You**: thanks
"""

_CODEX = """sparring-work #2

<!-- capture sid="01a07363-fdd1-7561-a7da-3681f2d06cdd" agent="codex" updated="x" schema="2" key="abc" -->

**You**: how should I open the conversation?

**Codex**: Lead with the constraint, not the ask.
"""

# A pre-schema capture: three-attribute header, no agent recorded.
_LEGACY = """some old run

<!-- capture sid="1775e9b6-a09c-41ba-abd9-424004d0fef9" updated="2026-06-16T18:49:19+02:00" -->

**You**: what changed?

**Assistant**: The alt-text pass ran twice.
"""


def test_claude_capture_yields_alternating_turns():
    parsed = parse_capture(_CLAUDE)
    assert parsed["agent"] == "claude"
    kinds = [e["kind"] for e in parsed["entries"]]
    assert kinds == ["user", "assistant", "user"]
    assert parsed["entries"][0]["text"] == "book the ferry for Friday"
    assert parsed["entries"][2]["text"] == "thanks"


def test_a_bold_heading_inside_a_reply_stays_in_that_reply():
    # Replies routinely contain their own "**Recommended fix**:" lines; only
    # the four speaker labels delimit a turn, or a reply would be shredded.
    reply = parse_capture(_CLAUDE)["entries"][1]["text"]
    assert reply.startswith("Booked the 07:40.")
    assert "**Recommended fix**: confirm the return leg." in reply


def test_codex_capture_is_read_the_same_way():
    parsed = parse_capture(_CODEX)
    assert parsed["agent"] == "codex"
    assert [e["kind"] for e in parsed["entries"]] == ["user", "assistant"]
    assert parsed["entries"][1]["text"] == "Lead with the constraint, not the ask."


def test_legacy_header_without_an_agent_still_parses():
    parsed = parse_capture(_LEGACY)
    assert parsed["agent"] == ""          # unknown stays unknown
    assert [e["kind"] for e in parsed["entries"]] == ["user", "assistant"]


def test_agent_falls_back_to_the_speaker_label_when_no_header_says():
    parsed = parse_capture("**You**: hi\n\n**Codex**: hello\n")
    assert parsed["agent"] == "codex"


def test_offsets_point_at_the_labels_in_the_original_text():
    for entry in parse_capture(_CLAUDE)["entries"]:
        assert _CLAUDE[entry["offset"]:].startswith("**")
    offsets = [e["offset"] for e in parse_capture(_CLAUDE)["entries"]]
    assert offsets == sorted(offsets)


def test_prose_with_no_speaker_labels_yields_nothing():
    # The caller reports this as unreadable rather than as an empty
    # conversation — the difference the viewer's fallback depends on.
    assert parse_capture("just a note someone left here\n")["entries"] == []


def test_windows_line_endings_and_empty_turns_are_normalised():
    text = "**You**: one\r\n\r\n**Claude**: \r\n\r\n**You**: two\r\n"
    entries = parse_capture(text)["entries"]
    assert [e["text"] for e in entries] == ["one", "two"]
