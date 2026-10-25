"""board.last_exchange and the launcher capture: tail JSONL parsing, typed-prompt detection and the bounded input log."""

from __future__ import annotations

from pathlib import Path


from src import board
from src.board import exchange as board_exchange

from tests.api.board._support import _assistant_line, _user_line, _write_jsonl


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


def test_last_exchange_skips_ismeta_string_content_lines(tmp_path: Path):
    """#1310: a string-content ``isMeta`` line — an ``[Image: ...]``
    placeholder, a harness re-invocation note — is Claude's own injected
    content, not something a person typed, even when its wording matches
    none of ``_SKIP_USER_PREFIXES``."""
    target = _write_jsonl(tmp_path / "t.jsonl", [
        _user_line("the real prompt"),
        {**_user_line("[Image: original 1290x2217, downscaled to 768x1290]"), "isMeta": True},
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


# ------------------------------------------------- has_typed_user_prompt (#670)

# The exact user-line shapes a launcher-spawned, never-talked-to session
# writes — verified against the real 2026-07-28 blank chief transcript: the
# slash-command wrapper is a plain string, the skill body it expands to rides
# as a content *list*, and the rename lands as a <system-reminder>.
_BOOTSTRAP_ONLY = [
    _user_line("<command-message>chief</command-message>\n"
               "<command-name>/chief</command-name>"),
    _user_line([{"type": "text", "text": "Base directory for this skill: ..."}]),
    _assistant_line([{"type": "text", "text": "reading the handover"}]),
    _user_line([{"type": "tool_result", "content": "..."}]),
    _user_line('<system-reminder>\nThe user named this session "chief".\n'
               "</system-reminder>"),
]


def test_typed_prompt_detected(tmp_path: Path):
    target = _write_jsonl(
        tmp_path / "t.jsonl", _BOOTSTRAP_ONLY + [_user_line("how is it going?")]
    )
    assert board.has_typed_user_prompt(target) is True


def test_bootstrap_only_transcript_is_a_confident_no(tmp_path: Path):
    target = _write_jsonl(tmp_path / "t.jsonl", _BOOTSTRAP_ONLY)
    assert board.has_typed_user_prompt(target) is False


def test_typed_prompt_ignores_ismeta_string_content_lines(tmp_path: Path):
    """#1310: same gap as ``last_exchange`` — a string-content ``isMeta``
    line (a harness re-invocation note, an image placeholder) must not read
    as a typed prompt just because it isn't one of ``_SKIP_USER_PREFIXES``."""
    target = _write_jsonl(tmp_path / "t.jsonl", _BOOTSTRAP_ONLY + [
        {**_user_line("(Re-invocation of /issue-add — the user's original "
                      "message is quoted above)"), "isMeta": True},
    ])
    assert board.has_typed_user_prompt(target) is False


def test_unreadable_transcript_is_unknown_not_no():
    assert board.has_typed_user_prompt(None) is None
    assert board.has_typed_user_prompt("") is None
    assert board.has_typed_user_prompt("C:/nope/missing.jsonl") is None


def test_oversized_tail_without_a_prompt_is_unknown_not_no(tmp_path: Path):
    """A file bigger than the tail window can hold a typed prompt the window
    never saw (a long autonomous stretch pushes it out of view) — that has to
    read as unknown, never as a confident "nothing was ever typed"."""
    filler = [_assistant_line([{"type": "text", "text": "x" * 4000}], f"m{i}")
              for i in range(80)]
    target = _write_jsonl(tmp_path / "t.jsonl", _BOOTSTRAP_ONLY + filler)
    assert Path(target).stat().st_size > 256 * 1024
    assert board.has_typed_user_prompt(target) is None


# ------------------------------------------ agent-aware source fallbacks (#457)


def test_launcher_exchange_ignores_coloured_tool_block_and_reads_input_log(
    tmp_path: Path,
):
    capture = tmp_path / "s.transcript"
    capture.write_text(
        "\x1b[32m● Ran a tool command\r\n"
        "  noisy tool result\r\n"
        "\x1b[39m● The actual assistant answer\r\n"
        "  continues on this line.\r\n",
        encoding="utf-8",
    )
    input_log = tmp_path / "s.log"
    input_log.write_text(
        "2026-07-02T12:00:00 [input] '\\x1b[200~full prompt\\x1b[201~'\n"
        "2026-07-02T12:00:01 [input] '\\r'\n",
        encoding="utf-8",
    )
    result = board_exchange.launcher_last_exchange(
        capture, launcher_input_path=input_log, rows=20, cols=80
    )
    assert result["source"] == "launcher"
    assert result["user"]["text"] == "full prompt"
    assert result["assistant"]["text"] == (
        "The actual assistant answer continues on this line."
    )


def _input_filler(n_bytes: int) -> str:
    """Non-submitting ``[input]`` lines (an arrow-key escape, stripped as CSI)
    totalling at least ``n_bytes`` — the traffic that piles up in a
    long-lived session's input log between submissions."""
    line = "2026-07-02T12:00:00 [input] '\\x1b[A'\n"
    return line * (n_bytes // len(line) + 1)


def test_launcher_input_log_read_is_bounded_to_its_tail(tmp_path: Path):
    """#881: ``webapp/sessions/<sid>.log`` grows forever and the chief drawer
    re-reads it every 5s, so only a bounded tail is scanned. A submission
    older than the window is out of reach by design (the caller falls back
    to the prompt title) — previously the whole file was slurped and walked
    character by character to find it."""
    input_log = tmp_path / "s.log"
    input_log.write_text(
        "2026-07-02T11:00:00 [input] 'ancient prompt\\r'\n"
        + _input_filler(board_exchange._INPUT_TAIL_BYTES),
        encoding="utf-8",
    )
    assert board_exchange._last_submitted_input(input_log) == ""


def test_launcher_input_log_tail_still_finds_the_recent_prompt(tmp_path: Path):
    """The bounded read keeps the common case: a prompt submitted within the
    tail window is found even behind more than a window's worth of older
    input, with the seek's torn first line dropped rather than parsed."""
    input_log = tmp_path / "s.log"
    input_log.write_text(
        _input_filler(board_exchange._INPUT_TAIL_BYTES * 2)
        + "2026-07-02T12:00:00 [input] 'recent prompt'\n"
        + "2026-07-02T12:00:01 [input] '\\r'\n",
        encoding="utf-8",
    )
    assert board_exchange._last_submitted_input(input_log) == "recent prompt"


def test_launcher_exchange_drops_grey_in_flight_tool_after_reply(tmp_path: Path):
    capture = tmp_path / "s.transcript"
    capture.write_text(
        "\x1b[38;2;255;255;255m● Completed assistant reply.\r\n"
        "\x1b[38;2;153;153;153m● Bash(pytest -q)\r\n"
        "  running…\r\n",
        encoding="utf-8",
    )
    result = board_exchange.launcher_last_exchange(
        capture, prompt_fallback="run the tests", rows=20, cols=80
    )
    assert result["assistant"]["text"] == "Completed assistant reply."


def test_launcher_exchange_new_session_is_true_empty(tmp_path: Path):
    capture = tmp_path / "new.transcript"
    capture.write_text("agent startup chrome only\r\n", encoding="utf-8")
    result = board_exchange.launcher_last_exchange(capture, rows=10, cols=40)
    assert result["available"] is False
    assert result["reason"] == "no_exchange"


def test_launcher_exchange_reads_only_the_bounded_capture_tail(
    tmp_path: Path, monkeypatch,
):
    capture = tmp_path / "bounded.transcript"
    capture.write_text(
        "● Old reply must fall outside the read window.\r\n"
        + ("x" * 200)
        + "\r\n● New reply is inside the bounded tail.\r\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(board_exchange, "_CAPTURE_TAIL_BYTES", 96)
    result = board_exchange.launcher_last_exchange(
        capture, prompt_fallback="latest?", rows=10, cols=80
    )
    assert result["assistant"]["text"] == "New reply is inside the bounded tail."
