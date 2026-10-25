"""Where a card's exchange comes from: Codex correlation, the scanned Claude history and its title check, and the state row the merge renders."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path


from src import board
from src.board import exchange as board_exchange
from src.session_view import transcript_locate

from tests.api.board._support import (
    NOW,
    _assistant_line,
    _iso,
    _live_sess,
    _user_line,
    _write_jsonl,
)


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
    monkeypatch.setattr(transcript_locate, "_CODEX_SESSIONS_DIR", sessions)
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


def test_codex_exchange_skips_harness_plumbing_as_the_prompt(tmp_path: Path):
    """#1422: the drawer shares ``codex_entries`` with the Chat reader, so an
    injected ``<environment_context>`` / ``<turn_aborted>`` user message is
    never shown as the last prompt."""
    def message(role: str, kind: str, text: str, ts: str) -> dict:
        return {"timestamp": ts, "type": "response_item",
                "payload": {"type": "message", "role": role,
                            "content": [{"type": kind, "text": text}]}}

    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("\n".join(json.dumps(item) for item in [
        message("user", "input_text", "real prompt", "t1"),
        message("user", "input_text", "<environment_context>cwd</environment_context>", "t2"),
        message("assistant", "output_text", "done", "t3"),
        message("user", "input_text", "<turn_aborted>x</turn_aborted>", "t4"),
    ]) + "\n", encoding="utf-8")
    result = board_exchange.codex_last_exchange(rollout)
    assert result["user"] == {"text": "real prompt", "timestamp": "t1"}
    assert result["assistant"] == {"text": "done", "timestamp": "t3"}


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
    monkeypatch.setattr(transcript_locate, "_CODEX_SESSIONS_DIR", sessions)
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
    slug = transcript_locate._CLAUDE_SLUG_RE.sub(
        "-", project_dir.replace("\\", "/").rstrip("/").lower()
    )
    folder = root / slug
    folder.mkdir(parents=True)
    monkeypatch.setattr(transcript_locate, "_CLAUDE_PROJECTS_DIR", root)
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
