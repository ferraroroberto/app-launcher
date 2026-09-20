"""Board drill-down (issue #301) — exchange parser, reply proxy, issue start.

Covers the act-from-the-card loop server-side:
  * ``board.last_exchange`` — tail JSONL parsing: text blocks joined across
    lines of the same assistant message, thinking/tool_use lines skipped,
    tool-result user lines skipped, harness wrappers skipped, missing file
    degraded to ``available: False``.
  * ``board.state_row_for_session`` — resolves the same row the board's
    merge renders (newest-session-wins claim order).
  * ``POST /api/claude-code/sessions/{sid}/input`` — one call to the
    session-host, which now owns framing + settle-then-submit itself
    (#611); the bare-submit escape hatch for a stranded composer.
  * ``POST /api/board/issues/start`` — server-built ``/issue-<mode> <N>``
    prompt, mode/number validation, repo resolution in the projects folder.
  * ``GET /api/board/sessions/{sid}/exchange`` — agent-aware native history,
    then the exact-id launcher capture fallback (#457).
  * passkey classification of all three new paths (gate refusal off-tailnet
    + ``_terminal_guard_level`` mapping).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src import board, board_exchange


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


def test_codex_native_exchange_correlates_by_unique_start_and_cwd(
    tmp_path: Path, monkeypatch,
):
    sessions = tmp_path / "sessions"
    local_start = datetime.fromtimestamp(NOW.timestamp()).astimezone()
    day = sessions / local_start.strftime("%Y/%m/%d")
    day.mkdir(parents=True)
    target_stamp = (local_start + timedelta(seconds=2)).strftime(
        "%Y-%m-%dT%H-%M-%S"
    )
    target = day / f"rollout-{target_stamp}-correct.jsonl"
    target.write_text("\n".join(json.dumps(item) for item in [
        {"type": "session_meta", "payload": {
            "timestamp": "2026-07-02T12:00:02Z", "cwd": "E:/proj/app",
        }},
        {"timestamp": "2026-07-02T12:01:00Z", "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "status?"}]}},
        {"timestamp": "2026-07-02T12:02:00Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "All green."}]}},
    ]) + "\n", encoding="utf-8")
    other_stamp = (local_start + timedelta(seconds=90)).strftime(
        "%Y-%m-%dT%H-%M-%S"
    )
    other = day / f"rollout-{other_stamp}-other.jsonl"
    other.write_text(
        '{"type":"session_meta","payload":{"cwd":"E:/proj/app"}}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(board_exchange, "_CODEX_SESSIONS_DIR", sessions)
    session = {
        "agent": "codex", "project_dir": "E:/proj/app",
        "started_at": NOW.timestamp(), "prompt_title": "status?",
    }
    result = board_exchange.resolve_exchange(
        session, None, tmp_path / "missing.transcript"
    )
    assert result["source"] == "codex"
    assert result["user"]["text"] == "status?"
    assert result["assistant"]["text"] == "All green."


def test_codex_ambiguous_native_match_degrades_to_exact_launcher_capture(
    tmp_path: Path, monkeypatch,
):
    sessions = tmp_path / "sessions"
    local_start = datetime.fromtimestamp(NOW.timestamp()).astimezone()
    day = sessions / local_start.strftime("%Y/%m/%d")
    day.mkdir(parents=True)
    for offset in (-1, 1):
        stamp = (local_start + timedelta(seconds=offset)).strftime(
            "%Y-%m-%dT%H-%M-%S"
        )
        (day / f"rollout-{stamp}-candidate.jsonl").write_text(
            '{"type":"session_meta","payload":{"cwd":"E:/proj/app"}}\n',
            encoding="utf-8",
        )
    capture = tmp_path / "exact.transcript"
    capture.write_text("● Exact session reply.\r\n", encoding="utf-8")
    monkeypatch.setattr(board_exchange, "_CODEX_SESSIONS_DIR", sessions)
    result = board_exchange.resolve_exchange({
        "agent": "codex", "project_dir": "E:/proj/app",
        "started_at": NOW.timestamp(), "prompt_title": "question",
    }, None, capture)
    assert result["source"] == "launcher"
    assert result["assistant"]["text"] == "Exact session reply."


# ------------------------------- rowless Claude scan fallback (#1027)
#
# Claude Code fires ``SessionEnd`` on ``/resume`` and ``/clear``, which
# deletes the hook state row out from under a still-live session, so the
# drawer resolves with no ``transcript_path`` until the next typed prompt.
# Before #1027 that dropped every such session to the PTY capture — and a
# detached one, which has no capture, to ``no_exchange`` outright.


def _claude_projects(tmp_path: Path, monkeypatch, project_dir: str) -> Path:
    """The ``~/.claude/projects`` folder Claude would file ``project_dir``
    under, redirected into ``tmp_path``. Mirrors the real naming: every
    non-alphanumeric of the normalized cwd replaced by ``-``."""
    root = tmp_path / "claude-projects"
    slug = board_exchange._CLAUDE_SLUG_RE.sub(
        "-", project_dir.replace("\\", "/").rstrip("/").lower()
    )
    folder = root / slug
    folder.mkdir(parents=True)
    monkeypatch.setattr(board_exchange, "_CLAUDE_PROJECTS_DIR", root)
    return folder


def _claude_session(sid: str = "s1", project_dir: str = "E:/proj/app", **over) -> dict:
    session = {
        "session_id": sid, "agent": "claude", "alive": True,
        "project_dir": project_dir, "started_at": NOW.timestamp(),
        "prompt_title": "earlier prompt",
    }
    session.update(over)
    return session


def _conversation(
    folder: Path, name: str, user: str, assistant: str,
    *, ai_title: str = "", custom_title: str = "",
) -> Path:
    """One conversation, optionally declaring its own name(s) (#1034).

    Claude Code writes both record types into the conversation itself and
    the window title follows ``custom-title`` when there is one, so a test
    can only exercise the disproof honestly by writing the same pair.
    """
    path = folder / name
    lines = [
        _user_line(user),
        _assistant_line([{"type": "text", "text": assistant}]),
    ]
    if ai_title:
        lines.append({"type": "ai-title", "aiTitle": ai_title})
    if custom_title:
        lines.append({"type": "custom-title", "customTitle": custom_title})
    _write_jsonl(path, lines)
    return path


def test_rowless_claude_session_prefers_scanned_history_over_capture(
    tmp_path: Path, monkeypatch,
):
    """#1027 criterion 1 + the ordering decision this issue was filed to
    settle: with no state row, the correlated JSONL outranks the capture.

    The capture here is perfectly parseable — this asserts a *ranking*,
    not a gap-fill. Structured chat data beats replayed terminal output.
    """
    folder = _claude_projects(tmp_path, monkeypatch, "E:/proj/app")
    _conversation(folder, "conv.jsonl", "what is left?", "Two items remain.")
    capture = tmp_path / "exact.transcript"
    capture.write_text("● Rougher capture rendering.\r\n", encoding="utf-8")

    session = _claude_session()
    result = board_exchange.resolve_exchange(session, None, capture, live=[session])

    assert result["source"] == "native_scan"
    assert result["assistant"]["text"] == "Two items remain."
    assert result["user"]["text"] == "what is left?"


def test_rowless_detached_claude_session_gets_an_exchange_not_no_exchange(
    tmp_path: Path, monkeypatch,
):
    """#1027 criterion 2. A ``RemoteSession`` has no launcher PTY capture,
    so before the scan a rowless detached session answered ``no_exchange``
    and the drawer's preview was simply empty."""
    folder = _claude_projects(tmp_path, monkeypatch, "E:/proj/detached")
    _conversation(folder, "conv.jsonl", "status?", "Detached and answering.")

    session = _claude_session(project_dir="E:/proj/detached", kind="remote")
    result = board_exchange.resolve_exchange(
        session, None, tmp_path / "absent.transcript", live=[session]
    )

    assert result["source"] == "native_scan"
    assert result["assistant"]["text"] == "Detached and answering."


def test_two_live_claude_sessions_in_one_dir_keep_the_rougher_capture(
    tmp_path: Path, monkeypatch,
):
    """#1027 criterion 3. Nothing in the folder says which of two live
    sessions owns the newest conversation, so the scan refuses and the
    honest-but-rougher capture stands — never the neighbour's text."""
    folder = _claude_projects(tmp_path, monkeypatch, "E:/proj/shared")
    _conversation(folder, "conv.jsonl", "whose?", "Ambiguous conversation.")
    capture = tmp_path / "exact.transcript"
    capture.write_text("● My own capture.\r\n", encoding="utf-8")

    mine = _claude_session("mine", project_dir="E:/proj/shared")
    sibling = _claude_session("sibling", project_dir="E:/proj/shared")
    result = board_exchange.resolve_exchange(
        mine, None, capture, live=[mine, sibling]
    )

    assert result["source"] == "launcher"
    assert result["assistant"]["text"] == "My own capture."
    assert "Ambiguous" not in json.dumps(result)


def test_declared_transcript_still_wins_and_is_never_relabelled(
    tmp_path: Path, monkeypatch,
):
    """#1027 criterion 4. The ordinary path is untouched: a session whose
    row names a transcript resolves through it and still reports the exact
    ``native``, with the scan never consulted."""
    folder = _claude_projects(tmp_path, monkeypatch, "E:/proj/app")
    _conversation(folder, "newer.jsonl", "scanned?", "Scanned conversation.")
    declared = tmp_path / "declared.jsonl"
    _write_jsonl(declared, [
        _user_line("declared?"),
        _assistant_line([{"type": "text", "text": "Declared conversation."}]),
    ])

    session = _claude_session()
    result = board_exchange.resolve_exchange(
        session, str(declared), tmp_path / "absent.transcript", live=[session]
    )

    assert result["source"] == "native"
    assert result["assistant"]["text"] == "Declared conversation."


def test_resume_window_without_a_pty_title_resolves_to_structured_history(
    tmp_path: Path, monkeypatch,
):
    """#1027's accepted trade, narrowed by #1034 to where it still applies.

    In the seconds after a ``/resume`` the newest file in the folder is the
    conversation the session just *left*, while the capture shows the one it
    resumed *into*. #1034 closes that window when the PTY title can settle
    it; this case is the remainder, where it cannot — no title was ever
    painted, so nothing outside the filesystem knows better. The #1027
    ranking then stands unchanged, and ``title_check`` says ``unknown``
    rather than implying a title agreed.
    """
    folder = _claude_projects(tmp_path, monkeypatch, "E:/proj/resumed")
    _conversation(folder, "left.jsonl", "old question", "Conversation just left.")
    capture = tmp_path / "exact.transcript"
    capture.write_text("● Conversation resumed into.\r\n", encoding="utf-8")

    session = _claude_session(project_dir="E:/proj/resumed", live_title="")
    result = board_exchange.resolve_exchange(session, None, capture, live=[session])

    assert result["source"] == "native_scan"
    assert result["source"] != "native"
    assert result["title_check"] == "unknown"
    assert result["assistant"]["text"] == "Conversation just left."


def test_scan_is_claude_only_and_leaves_other_agents_alone(
    tmp_path: Path, monkeypatch,
):
    """The scan is keyed on Claude's own project-folder layout, so another
    agent sitting in the same cwd must never be handed a Claude JSONL."""
    folder = _claude_projects(tmp_path, monkeypatch, "E:/proj/app")
    _conversation(folder, "conv.jsonl", "q", "Claude's conversation.")
    capture = tmp_path / "exact.transcript"
    capture.write_text("● Grok capture.\r\n", encoding="utf-8")

    session = _claude_session(agent="grok")
    result = board_exchange.resolve_exchange(session, None, capture, live=[session])

    assert result["source"] == "launcher"
    assert result["assistant"]["text"] == "Grok capture."


# --------------------------------------------------------- #1034 disproof


def test_pty_title_disproves_the_scan_and_the_capture_answers_instead(
    tmp_path: Path, monkeypatch,
):
    """#1034 criterion 1, and the whole point of the issue.

    The real ``/resume`` window: the newest file in the folder is the
    conversation the session just left ("Old billing bug"), while the PTY
    title names the one it resumed into. They conflict, so the scan is
    refused and the exact-id capture — which shows what is actually on
    screen — answers instead. Before this change the drawer showed the
    left conversation's text under the live session's name.
    """
    folder = _claude_projects(tmp_path, monkeypatch, "E:/proj/resumed")
    _conversation(folder, "left.jsonl", "old question", "Conversation just left.",
                  ai_title="Old billing bug")
    capture = tmp_path / "exact.transcript"
    capture.write_text("● Conversation resumed into.\r\n", encoding="utf-8")

    session = _claude_session(project_dir="E:/proj/resumed",
                              live_title="◐ Issue 1034")
    result = board_exchange.resolve_exchange(session, None, capture, live=[session])

    assert result["source"] == "launcher"
    assert result["title_check"] == "disproved"
    assert result["assistant"]["text"] == "Conversation resumed into."
    assert "Old billing bug" not in json.dumps(result)
    assert "just left" not in json.dumps(result)


def test_an_agreeing_title_leaves_the_scan_ranking_exactly_as_it_was(
    tmp_path: Path, monkeypatch,
):
    """#1034 criterion 2. Agreement changes nothing — same source, same
    text — and the busy spinner glyph is chrome, not part of the name."""
    folder = _claude_projects(tmp_path, monkeypatch, "E:/proj/app")
    _conversation(folder, "conv.jsonl", "what is left?", "Two items remain.",
                  ai_title="Issue 1034")
    capture = tmp_path / "exact.transcript"
    capture.write_text("● Rougher capture rendering.\r\n", encoding="utf-8")

    session = _claude_session(live_title="◐ Issue 1034")
    result = board_exchange.resolve_exchange(session, None, capture, live=[session])

    assert result["source"] == "native_scan"
    assert result["title_check"] == "corroborated"
    assert result["assistant"]["text"] == "Two items remain."


def test_idle_glyph_is_stripped_from_the_title_like_the_busy_one(
    tmp_path: Path, monkeypatch,
):
    """A live PTY sits on the *idle* glyph whenever control is at the
    prompt, which is most of the time — 2 of the 3 PTY sessions live on
    this box while #1034 was built. Stripping only the busy spinner (all
    the issue asked for) would make every idle session disagree with its
    own conversation and permanently demote it to the capture."""
    folder = _claude_projects(tmp_path, monkeypatch, "E:/proj/app")
    _conversation(folder, "conv.jsonl", "q", "Idle but correct.",
                  ai_title="Wooden floor repair investigation")
    capture = tmp_path / "exact.transcript"
    capture.write_text("● Capture.\r\n", encoding="utf-8")

    session = _claude_session(
        live_title="✳ Wooden floor repair investigation"
    )
    result = board_exchange.resolve_exchange(session, None, capture, live=[session])

    assert result["title_check"] == "corroborated"
    assert result["source"] == "native_scan"


def test_a_custom_title_is_accepted_even_when_the_ai_title_differs(
    tmp_path: Path, monkeypatch,
):
    """The standing fleet chief, and why ``ai-title`` alone was not enough.

    Claude Code paints ``custom-title`` in the window while ``ai-title``
    stays frozen at whatever it inferred early — the real chief carries
    ``customTitle: "chief"`` and ``aiTitle: "Investigate and fix failed
    jobs"`` in the same file. Matching against ``ai-title`` only, as #1034
    proposed, would have refused the chief's own conversation on every
    poll, forever.
    """
    folder = _claude_projects(tmp_path, monkeypatch, "E:/proj/app")
    _conversation(folder, "conv.jsonl", "status?", "Chief reporting.",
                  ai_title="Investigate and fix failed jobs",
                  custom_title="chief")
    capture = tmp_path / "exact.transcript"
    capture.write_text("● Capture.\r\n", encoding="utf-8")

    session = _claude_session(live_title="✳ chief")
    result = board_exchange.resolve_exchange(session, None, capture, live=[session])

    assert result["title_check"] == "corroborated"
    assert result["source"] == "native_scan"
    assert result["assistant"]["text"] == "Chief reporting."


def test_an_unnamed_conversation_is_unknown_and_is_never_refused(
    tmp_path: Path, monkeypatch,
):
    """#1034 criterion 6 — the direction test.

    Claude Code names a conversation only once it has enough content to
    name one, so a fresh or bootstrap-only conversation declares nothing.
    Agreement must therefore never be *required*: only a genuine conflict
    refuses. If this ever flips to ``launcher`` the check has become a
    confirmation, which is the failure mode #1034 was written to avoid.
    """
    folder = _claude_projects(tmp_path, monkeypatch, "E:/proj/app")
    _conversation(folder, "conv.jsonl", "q", "Unnamed but correct.")
    capture = tmp_path / "exact.transcript"
    capture.write_text("● Capture.\r\n", encoding="utf-8")

    session = _claude_session(live_title="◐ Issue 1034")
    result = board_exchange.resolve_exchange(session, None, capture, live=[session])

    assert result["source"] == "native_scan"
    assert result["title_check"] == "unknown"
    assert result["assistant"]["text"] == "Unnamed but correct."


def test_a_detached_session_has_no_title_and_keeps_its_only_source(
    tmp_path: Path, monkeypatch,
):
    """#1034 criterion 3. A ``RemoteSession`` has no PTY, so ``live_title``
    is always ``""`` — and detached sessions are exactly the ones that had
    no fallback at all before #1027. A check that refused for lack of a
    signal they can never have would take away their only source."""
    folder = _claude_projects(tmp_path, monkeypatch, "E:/proj/detached")
    _conversation(folder, "conv.jsonl", "status?", "Detached and answering.",
                  ai_title="Some other name entirely")

    session = _claude_session(project_dir="E:/proj/detached", kind="remote",
                              live_title="")
    result = board_exchange.resolve_exchange(
        session, None, tmp_path / "absent.transcript", live=[session]
    )

    assert result["source"] == "native_scan"
    assert result["title_check"] == "unknown"
    assert result["assistant"]["text"] == "Detached and answering."


def test_an_exact_row_never_consults_the_title_check_at_all(
    tmp_path: Path, monkeypatch,
):
    """``title_check`` is present exactly when the scan was consulted, so a
    session whose row names a transcript carries no verdict — the field's
    presence is itself the signal that a correlation was inferred."""
    folder = _claude_projects(tmp_path, monkeypatch, "E:/proj/app")
    _conversation(folder, "newer.jsonl", "scanned?", "Scanned conversation.",
                  ai_title="Totally different")
    declared = tmp_path / "declared.jsonl"
    _write_jsonl(declared, [
        _user_line("declared?"),
        _assistant_line([{"type": "text", "text": "Declared conversation."}]),
    ])

    session = _claude_session(live_title="◐ Issue 1034")
    result = board_exchange.resolve_exchange(
        session, str(declared), tmp_path / "absent.transcript", live=[session]
    )

    assert result["source"] == "native"
    assert "title_check" not in result


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
    """#537: an ambiguous single row (2 live sessions sharing one cwd)
    matches neither — state_row_for_session must stay consistent with what
    merge_sessions renders rather than resolving the ambiguity differently."""
    live = [_live_sess("old", "E:/a/x", 120), _live_sess("new", "E:/a/x", 5)]
    rows = {
        "t1": {"cwd": "E:/a/x", "status": "needs-you",
               "updated_at": _iso(NOW - timedelta(minutes=1)),
               "transcript_path": "p1"},
    }
    assert board.state_row_for_session(live, rows, "new") is None
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

    def test_multiline_forwarded_raw_in_one_call(self, webapp_client, _bypass_gate):
        """Framing + the submit CR are the session-host's own job now
        (#611) — the router just forwards data + submit in a single call."""
        client, _, overrides = webapp_client
        resp = client.post(
            "/api/claude-code/sessions/s1/input",
            json={"data": "line one\nline two", "submit": True},
        )
        assert resp.status_code == 200
        calls = overrides["session"].send_input.call_args_list
        assert len(calls) == 1
        assert calls[0].args == (8446, "s1", "line one\nline two", True)

    def test_single_line_forwarded_raw(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        client.post(
            "/api/claude-code/sessions/s1/input",
            json={"data": "hello", "submit": True},
        )
        calls = overrides["session"].send_input.call_args_list
        assert len(calls) == 1
        assert calls[0].args == (8446, "s1", "hello", True)

    def test_no_submit_forwards_submit_false(self, webapp_client, _bypass_gate):
        client, _, overrides = webapp_client
        client.post(
            "/api/claude-code/sessions/s1/input",
            json={"data": "draft", "submit": False},
        )
        calls = overrides["session"].send_input.call_args_list
        assert len(calls) == 1
        assert calls[0].args == (8446, "s1", "draft", False)

    def test_blank_data_without_submit_is_400(self, webapp_client, _bypass_gate):
        """Blank data with no submit is a genuine no-op request — nothing
        to write, nothing to submit — unlike the bare-submit escape hatch
        below, which always carries submit=True."""
        client, _, _ = webapp_client
        assert client.post(
            "/api/claude-code/sessions/s1/input",
            json={"data": "   ", "submit": False},
        ).status_code == 400
        assert client.post(
            "/api/claude-code/sessions/s1/input",
            json={"data": "", "submit": False},
        ).status_code == 400

    def test_bare_submit_escape_hatch_releases_stranded_composer(
        self, webapp_client, _bypass_gate
    ):
        """{"data": "", "submit": true} (#611) — release whatever is already
        sitting in the composer, with no text write. The recovery path for a
        message stranded by the submit race, previously only reachable by
        tapping the phone's own compose Send by hand."""
        client, _, overrides = webapp_client
        resp = client.post(
            "/api/claude-code/sessions/s1/input",
            json={"data": "", "submit": True},
        )
        assert resp.status_code == 200
        calls = overrides["session"].send_input.call_args_list
        assert len(calls) == 1
        assert calls[0].args == (8446, "s1", "", True)

    def test_whitespace_only_data_with_submit_is_bare_submit(
        self, webapp_client, _bypass_gate
    ):
        """Whitespace-only data collapses to the same bare-submit call as
        an empty string — there is no meaningful text to write either way."""
        client, _, overrides = webapp_client
        resp = client.post(
            "/api/claude-code/sessions/s1/input",
            json={"data": "   ", "submit": True},
        )
        assert resp.status_code == 200
        calls = overrides["session"].send_input.call_args_list
        assert calls[0].args == (8446, "s1", "", True)

    def test_dead_session_surfaces_as_error_not_false_ok(
        self, webapp_client, _bypass_gate
    ):
        """A session-host 409 (write dropped, issue #607) must propagate as a
        real error — never collapse back to {"ok": true}."""
        client, _, overrides = webapp_client
        overrides["session"].send_input.side_effect = (
            overrides["session"].SessionHostError(
                "session s1 not accepting input (exited)", status=409
            )
        )

        resp = client.post(
            "/api/claude-code/sessions/s1/input",
            json={"data": "hello", "submit": True},
        )

        assert resp.status_code == 409
        assert resp.json() != {"ok": True}


class TestSessionInputStaleHost:
    """#967 reopen: a detached Send against a session-host that predates
    detached-session input came back as a bare "session-host HTTP 500".
    The route now names that condition — and only that condition — from
    facts it can establish (the target's kind, the host's loaded sha),
    keeping "stale host", "new path failed" and "couldn't tell" distinct."""

    _FEATURE = "1cf17209c86885ff3a488bd2501e26e8a69c4580"

    def _post_after_500(self, webapp_client, monkeypatch, *, kind, contains):
        from app.webapp.routers import sessions as sessions_router

        client, _, overrides = webapp_client
        sess = overrides["session"]
        sess.send_input.side_effect = sess.SessionHostError(
            "session-host HTTP 500", status=500
        )
        sess.get_session.return_value = {"session_id": "sid-967", "kind": kind}
        sess.identity.return_value = {
            "git_sha": "5ea6ef8", "started_at": "2026-09-13T09:36:27",
        }
        calls = []

        def _contains(repo, sha, commit):
            calls.append((sha, commit))
            return contains

        monkeypatch.setattr(
            sessions_router, "sha_contains_commit", _contains, raising=False
        )
        resp = client.post(
            "/api/claude-code/sessions/sid-967/input",
            json={"data": "what is this project?", "submit": True},
        )
        return resp, calls

    def test_detached_send_to_a_predating_host_names_the_restart(
        self, webapp_client, _bypass_gate, monkeypatch
    ):
        resp, calls = self._post_after_500(
            webapp_client, monkeypatch, kind="remote", contains=False
        )
        assert resp.status_code == 501
        detail = resp.json()["detail"]
        assert "session-host restart needed" in detail
        assert "5ea6ef8" in detail
        assert "HTTP 500" not in detail
        assert calls == [("5ea6ef8", self._FEATURE)]

    def test_detached_send_failing_on_a_current_host_passes_the_real_error(
        self, webapp_client, _bypass_gate, monkeypatch
    ):
        # The new path genuinely failed — never mislabel that as "restart".
        resp, _ = self._post_after_500(
            webapp_client, monkeypatch, kind="remote", contains=True
        )
        assert resp.status_code == 500
        assert resp.json()["detail"] == "session-host HTTP 500"

    def test_unknown_host_lineage_is_reported_as_unknown_not_stale(
        self, webapp_client, _bypass_gate, monkeypatch
    ):
        resp, _ = self._post_after_500(
            webapp_client, monkeypatch, kind="remote", contains=None
        )
        assert resp.status_code == 500
        detail = resp.json()["detail"]
        assert detail.startswith("session-host HTTP 500")
        assert "could not confirm" in detail
        assert "restart needed" not in detail

    def test_pty_target_is_never_classified(
        self, webapp_client, _bypass_gate, monkeypatch
    ):
        # Every session-host has had PTY input — a PTY 500 is not staleness.
        resp, calls = self._post_after_500(
            webapp_client, monkeypatch, kind="pty", contains=False
        )
        assert resp.status_code == 500
        assert resp.json()["detail"] == "session-host HTTP 500"
        assert calls == []

    def test_pinned_commit_is_the_one_that_added_detached_input(self):
        # A typo'd pin would silently degrade every detached 500 to
        # "could not confirm". CI's depth-1 checkout lacks the object.
        import subprocess

        from app.webapp.routers import sessions as sessions_router

        repo = Path(__file__).resolve().parent.parent
        pin = sessions_router._DETACHED_INPUT_COMMIT
        assert pin == self._FEATURE

        def show(rev: str) -> "str | None":
            out = subprocess.run(
                ["git", "-C", str(repo), "show", f"{rev}:src/session_host.py"],
                capture_output=True, text=True, encoding="utf-8",
            )
            return out.stdout if out.returncode == 0 else None

        at_pin = show(pin)
        if at_pin is None:
            pytest.skip("pinned commit not in this (shallow) clone")
        before = show(f"{pin}~1")
        assert "class RemoteInputOutcome" in at_pin
        assert before is not None and "class RemoteInputOutcome" not in before

    def test_non_500_errors_skip_the_lineage_check(
        self, webapp_client, _bypass_gate, monkeypatch
    ):
        from app.webapp.routers import sessions as sessions_router

        client, _, overrides = webapp_client
        sess = overrides["session"]
        sess.send_input.side_effect = sess.SessionHostError(
            "session sid-967 not accepting input (exited)", status=409
        )
        monkeypatch.setattr(
            sessions_router, "sha_contains_commit",
            lambda *a: pytest.fail("lineage must not be checked for a 409"),
            raising=False,
        )
        resp = client.post(
            "/api/claude-code/sessions/sid-967/input",
            json={"data": "hi", "submit": True},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"] == "session sid-967 not accepting input (exited)"
        sess.get_session.assert_not_called()


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

    @pytest.mark.parametrize("model", ["sonnet", "opus", "fable"])
    def test_model_overrides_persisted_coding_model(
        self, webapp_client, _bypass_gate, _spawn, model
    ):
        """#505: the dispatch bar's selector governs one-tap starts too."""
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        client.post(
            "/api/board/issues/start",
            json={"repo": "myrepo", "number": 9, "mode": "start",
                  "model": model},
        )
        assert f"--model {model}" in _spawn["flags"]
        assert _spawn["flags"].endswith(' "/issue-start 9"')
        assert _spawn["agent"] == "claude"

    def test_absent_model_keeps_persisted_coding_model(
        self, webapp_client, _bypass_gate, _spawn
    ):
        """No ``model`` (stale-cache client) → legacy behaviour: the
        persisted Coding model (opus in the test config), unchanged."""
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        client.post(
            "/api/board/issues/start",
            json={"repo": "myrepo", "number": 9, "mode": "start"},
        )
        assert "--model opus" in _spawn["flags"]
        assert _spawn["agent"] == "claude"

    def test_explicit_codex_model_starts_with_positional_prompt(
        self, webapp_client, _bypass_gate, _spawn, monkeypatch
    ):
        """#845: an exact Codex model carries shared flags and the same
        server-built ``/issue-*`` positional prompt appended (Codex takes
        ``codex [OPTIONS] [PROMPT]`` like claude)."""
        from app.webapp.routers import board_spawn
        monkeypatch.setattr(
            board_spawn.agents, "is_installed", lambda a: a == "codex"
        )
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        resp = client.post(
            "/api/board/issues/start",
            json={"repo": "myrepo", "number": 7, "mode": "yolo",
                  "model": "codex:gpt-5.6-sol"},
        )
        assert resp.status_code == 200
        assert _spawn["agent"] == "codex" and _spawn["kind"] == "pty"
        assert "model_reasoning_effort=" in _spawn["flags"]
        assert "--model gpt-5.6-sol" in _spawn["flags"]
        assert _spawn["flags"].endswith(' "/issue-yolo 7"')

    def test_gpt56_without_codex_installed_400s(
        self, webapp_client, _bypass_gate, _spawn, monkeypatch
    ):
        from app.webapp.routers import board_spawn
        monkeypatch.setattr(
            board_spawn.agents, "is_installed", lambda a: False
        )
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        resp = client.post(
            "/api/board/issues/start",
            json={"repo": "myrepo", "number": 7, "mode": "start",
                  "model": "codex:gpt-5.6-sol"},
        )
        assert resp.status_code == 400
        assert "not installed" in resp.json()["detail"]
        assert not _spawn

    def test_unknown_model_400s(self, webapp_client, _bypass_gate, _spawn):
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        resp = client.post(
            "/api/board/issues/start",
            json={"repo": "myrepo", "number": 7, "mode": "start",
                  "model": "haiku"},
        )
        assert resp.status_code == 400
        assert "unknown model" in resp.json()["detail"]
        assert not _spawn

    def test_unknown_repo_is_404(self, webapp_client, _bypass_gate, _spawn):
        client, _, _ = webapp_client
        resp = client.post(
            "/api/board/issues/start",
            json={"repo": "not-checked-out", "number": 1, "mode": "start"},
        )
        assert resp.status_code == 404

    def test_title_auto_names_the_spawned_session(
        self, webapp_client, _bypass_gate, _spawn
    ):
        """#467: a Board start carrying the issue title renames the spawned
        session after it, via the #458 manual-override path (a launcher-side
        ``manual_title`` set — no PTY typing, so no readiness wait)."""
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        resp = client.post(
            "/api/board/issues/start",
            json={
                "repo": "myrepo", "number": 42, "mode": "start",
                "title": "Board tab: auto-name a started session",
            },
        )
        assert resp.status_code == 200
        assert '--name "Board tab: auto-name a started session"' in _spawn["flags"]
        overrides["session"].rename.assert_called_once_with(
            8446, "spawned-1", "Board tab: auto-name a started session"
        )

    def test_unsafe_title_remains_launcher_only(
        self, webapp_client, _bypass_gate, _spawn
    ):
        """A shell-sensitive issue title never reaches a native CLI flag."""
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        title = "docs & release"
        resp = client.post(
            "/api/board/issues/start",
            json={"repo": "myrepo", "number": 42, "mode": "start", "title": title},
        )
        assert resp.status_code == 200
        assert "--name" not in _spawn["flags"]
        overrides["session"].rename.assert_called_once_with(8446, "spawned-1", title)

    def test_codex_title_remains_launcher_only(
        self, webapp_client, _bypass_gate, _spawn, monkeypatch
    ):
        """Codex exposes no verified spawn-time session-name interface."""
        from app.webapp.routers import board_spawn

        monkeypatch.setattr(
            board_spawn.agents, "is_installed", lambda agent: agent == "codex"
        )
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        resp = client.post(
            "/api/board/issues/start",
            json={
                "repo": "myrepo", "number": 42, "mode": "start",
                "model": "codex:gpt-5.6-luna", "title": "Codex title",
            },
        )
        assert resp.status_code == 200
        assert "--name" not in _spawn["flags"]
        overrides["session"].rename.assert_called_once_with(
            8446, "spawned-1", "Codex title"
        )

    def test_blank_title_skips_rename(
        self, webapp_client, _bypass_gate, _spawn
    ):
        """No title (or whitespace-only) → no rename call; the session keeps
        its automatic title precedence."""
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        resp = client.post(
            "/api/board/issues/start",
            json={"repo": "myrepo", "number": 42, "mode": "start", "title": "   "},
        )
        assert resp.status_code == 200
        overrides["session"].rename.assert_not_called()

    def test_rename_failure_does_not_fail_the_launch(
        self, webapp_client, _bypass_gate, _spawn
    ):
        """A rename error is best-effort — the launch still succeeds (#467)."""
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        overrides["session"].rename.side_effect = (
            overrides["session"].SessionHostError("boom", 502)
        )
        resp = client.post(
            "/api/board/issues/start",
            json={
                "repo": "myrepo", "number": 42, "mode": "yolo",
                "title": "some issue title",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["session"]["session_id"] == "spawned-1"


# ---------------------------------------------- chief-managed marking (#474)
#
# `start_issue` is the path chief actually calls over loopback -- never
# through `chief_ops.py dispatch`'s own CLI-side marking, so without this
# the worker it spawns never got a `chief-managed.json` entry. Gated on a
# live chief PTY session being present so a human driving the Board on this
# same machine never gets their own dispatch marked.


class TestChiefManagedMarking:

    @pytest.fixture
    def _spawn(self, webapp_client, monkeypatch):
        from app.webapp.routers import board as board_router
        captured: dict = {}

        def fake_spawn(project_dir, name, flags, port, kind, agent, rows, cols,
                       history_lines=None):
            captured.update(name=name)
            return {"session_id": "spawned-1", "kind": "pty", "name": name}

        monkeypatch.setattr(board_router, "spawn_claude_session", fake_spawn)
        return captured

    @pytest.fixture
    def _fleet_config_repo(self, webapp_client):
        """A resolvable ``fleet-config`` project dir with a fake venv python
        and `chief_managed.py`, so `_resolve_repo_entry` + the two
        `.exists()` checks in `_mark_chief_managed` succeed without ever
        really invoking a subprocess (`subprocess.run` is faked below)."""
        _, _, overrides = webapp_client
        repo = overrides["tmp_projects_dir"] / "fleet-config"
        (repo / ".venv" / "Scripts").mkdir(parents=True)
        (repo / ".venv" / "Scripts" / "python.exe").touch()
        (repo / "skills" / "_lib").mkdir(parents=True)
        (repo / "skills" / "_lib" / "chief_managed.py").touch()
        return repo

    @pytest.fixture
    def _fake_run(self, monkeypatch):
        from app.webapp.routers import board_chief
        calls: list = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return MagicMock(returncode=0)

        monkeypatch.setattr(board_chief.subprocess, "run", fake_run)
        return calls

    def _set_live_sessions(self, overrides, sessions):
        overrides["session"].list_sessions.return_value = sessions

    def test_marks_when_chief_alive_and_loopback(
        self, webapp_client, _bypass_gate, _spawn, _fleet_config_repo, _fake_run,
    ):
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        self._set_live_sessions(overrides, [
            {"session_id": "chief-1", "kind": "pty", "alive": True, "label": "chief"},
        ])
        resp = client.post(
            "/api/board/issues/start",
            json={"repo": "myrepo", "number": 42, "mode": "start"},
        )
        assert resp.status_code == 200
        assert len(_fake_run) == 1
        cmd = _fake_run[0]
        assert cmd[2:] == ["mark", "spawned-1", "myrepo", "42"]
        assert cmd[0].endswith("python.exe")
        assert cmd[1].endswith("chief_managed.py")

    def test_does_not_mark_without_a_live_chief_session(
        self, webapp_client, _bypass_gate, _spawn, _fleet_config_repo, _fake_run,
    ):
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        self._set_live_sessions(overrides, [])  # no chief session alive
        resp = client.post(
            "/api/board/issues/start",
            json={"repo": "myrepo", "number": 42, "mode": "start"},
        )
        assert resp.status_code == 200
        assert _fake_run == []

    def test_marking_failure_does_not_fail_the_launch(
        self, webapp_client, _bypass_gate, _spawn, _fleet_config_repo, monkeypatch,
    ):
        """Mirrors `test_rename_failure_does_not_fail_the_launch` -- a
        subprocess error while marking is best-effort, never fatal to the
        dispatch (matches `chief_ops.py cmd_dispatch`'s own try/except)."""
        from app.webapp.routers import board_chief

        def raising_run(cmd, **kwargs):
            raise OSError("boom")

        monkeypatch.setattr(board_chief.subprocess, "run", raising_run)
        client, _, overrides = webapp_client
        (overrides["tmp_projects_dir"] / "myrepo").mkdir()
        self._set_live_sessions(overrides, [
            {"session_id": "chief-1", "kind": "pty", "alive": True, "label": "chief"},
        ])
        resp = client.post(
            "/api/board/issues/start",
            json={"repo": "myrepo", "number": 42, "mode": "start"},
        )
        assert resp.status_code == 200
        assert resp.json()["session"]["session_id"] == "spawned-1"


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
        assert body["available"] is False
        assert body["reason"] == "session_not_found"

    @pytest.mark.parametrize("agent, with_missing_native", [
        ("claude", True),
        ("codex", False),
    ])
    def test_launcher_capture_falls_back_when_native_exchange_is_unavailable(
        self, webapp_client, _bypass_gate, tmp_path: Path, monkeypatch,
        agent: str, with_missing_native: bool,
    ):
        """#457 repro: both a missing Claude hook transcript and a Codex
        session with no hook row still have an exact-id launcher capture."""
        from app.webapp.routers import board as board_router

        client, app, overrides = webapp_client
        capture = tmp_path / "sess1.transcript"
        capture.write_text(
            "\x1b[39m\u2022 The exact launcher capture has the latest reply.\r\n"
            "  It remains linked by session id.\r\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            board_router.audit, "transcript_path", lambda _sid: capture
        )

        live = _live_sess("sess1", "E:/proj/app", 10)
        live.update(agent=agent, prompt_title="show the latest exchange")
        overrides["session"].list_sessions.return_value = [live]

        if with_missing_native:
            state_file = Path(app.state.webapp_config.sessions_state_file)
            state_file.write_text(json.dumps({
                "t-uuid": {
                    "agent": "claude", "cwd": "E:/proj/app",
                    "status": "needs-you", "updated_at": _iso(NOW),
                    "transcript_path": str(tmp_path / "missing.jsonl"),
                },
            }), encoding="utf-8")

        body = client.get("/api/board/sessions/sess1/exchange").json()
        assert body["available"] is True
        assert body["source"] == "launcher"
        assert body["user"]["text"] == "show the latest exchange"
        assert body["assistant"]["text"] == (
            "The exact launcher capture has the latest reply. "
            "It remains linked by session id."
        )
