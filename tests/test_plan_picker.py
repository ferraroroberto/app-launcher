"""Chat's plan card answering (#1151): reading Claude Code's plan picker off
the terminal screen, and the keys a tap turns into.

The screens here are synthetic, laid out like the picker probed on Claude
Code 2.1.281 (a plan-mode session at 120 columns, a skip-permissions one at
46): a solid rule, the question paragraph, numbered options with the
feedback field's hint under its label, and the ``ctrl+g`` footer naming the
plan file. Keys, as probed: a "Yes" digit approves on its own; the feedback
digit focuses a text field, the text follows, Enter sends the plan back.

The same screen read serves the overlay's context ring (#1223): the fleet
statusline's ``NN%c`` under Claude Code's prompt, tested at the end.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from src import plan_picker as pp
from src.statusline_context import context_percent

RULE = "\u2500" * 40
DASH = "\u254c" * 40
CUR = "\u276f"


def _screen(options: List[str], below: List[str] = None, plan: List[str] = None) -> List[str]:
    lines = [
        "\u25cf Updated plan",
        RULE,
        " Ready to code?",
        "",
        " Here is Claude's plan:",
        DASH,
        *(plan if plan is not None else [" Make a file", "", " 1. Write it", " 2. Check it"]),
        DASH,
        "",
        RULE,
        " Claude has written up a plan and is ready to",
        " execute. Would you like to proceed?",
        "",
        *options,
        "",
        *(below if below is not None else [
            " ctrl+g to edit in Notepad \u00b7 C:\\Users\\me\\.",
            " claude\\plans\\tidy-blue-otter.md",
        ]),
        "", "",
    ]
    return lines


PLAN_MODE = [
    f" {CUR} 1. Yes, auto-accept edits",
    "   2. Yes, manually approve edits",
    "   3. Tell Claude what to change",
    "      shift+tab to approve with this feedback",
]
# 46 columns: the bypass label wraps onto a row indented to its own column.
BYPASS_NARROW = [
    f" {CUR} 1. Yes, and switch to BYPASS PERMISSIONS",
    "      (no further prompts) for this session",
    "   2. Yes, manually approve edits",
    "   3. Tell Claude what to change",
    "      shift+tab to approve with this feedback",
]


# ------------------------------------------------------------------ reading


def test_the_picker_reads_as_the_screen_lists_it():
    p = pp.parse_picker(_screen(PLAN_MODE))
    assert p["options"] == [
        {"n": 1, "label": "Yes, auto-accept edits", "kind": "approve"},
        {"n": 2, "label": "Yes, manually approve edits", "kind": "approve"},
        {"n": 3, "label": "Tell Claude what to change", "kind": "feedback"},
    ]
    assert p["cursor"] == 1 and p["answerable"] is True
    # The footer's path wraps mid-name; only the bare name is kept.
    assert p["plan_file"] == "tidy-blue-otter.md"
    assert p["plan_excerpt"] == "Make a file\n\n1. Write it\n2. Check it"


def test_a_wrapped_label_is_joined_and_the_hint_is_not_part_of_it():
    p = pp.parse_picker(_screen(BYPASS_NARROW))
    assert p["options"][0]["label"] == (
        "Yes, and switch to BYPASS PERMISSIONS (no further prompts) for this session"
    )
    assert p["options"][2] == {"n": 3, "label": "Tell Claude what to change", "kind": "feedback"}


def test_the_feedback_field_in_focus_leaves_nothing_answerable():
    """With the cursor on the text field a digit is typed as text, and once
    something is typed the option's label is that text."""
    focused = [
        "   1. Yes, auto-accept edits",
        "   2. Yes, manually approve edits",
        f" {CUR} 3. Tell Claude what to change",
        "      shift+tab to approve with this feedback",
    ]
    p = pp.parse_picker(_screen(focused))
    assert p["cursor"] == 3 and p["answerable"] is False
    typed = [
        "   1. Yes, auto-accept edits",
        f" {CUR} 2. Yes, manually approve edits",
        "   3. use the word hello",
        "      shift+tab to approve with this feedback",
    ]
    p = pp.parse_picker(_screen(typed))
    assert p["answerable"] is True
    assert p["options"][2]["kind"] == "other"


def test_a_plan_taller_than_the_screen_has_no_excerpt():
    lines = _screen(PLAN_MODE)
    top = lines.index(DASH)
    del lines[: top + 1]  # the heading and the upper rule scrolled off
    p = pp.parse_picker(lines)
    assert p is not None and p["plan_excerpt"] == ""


@pytest.mark.parametrize("screen,why", [
    (["", " Nothing to see", ""], "no question on screen"),
    (_screen(PLAN_MODE, below=[" \u273d Nesting\u2026 (esc to interrupt)"]), "the agent moved on"),
    (_screen(PLAN_MODE, below=[RULE, f"{CUR} ", RULE]), "a composer under the options"),
    (_screen(PLAN_MODE, below=[" ctrl+g to edit in Notepad", "", " a new reply"]), "text under the footer"),
    (_screen(PLAN_MODE[:3]), "no feedback option: not the plan picker"),
    (_screen([PLAN_MODE[0], PLAN_MODE[2].replace("3.", "4."), PLAN_MODE[3]]), "numbering gap"),
    (_screen(PLAN_MODE[:1]), "one option"),
])
def test_anything_else_below_the_options_reads_as_not_showing(screen, why):
    assert pp.parse_picker(screen) is None, why


def test_no_footer_is_fine_and_names_no_plan_file():
    p = pp.parse_picker(_screen(PLAN_MODE, below=[]))
    assert p is not None and p["plan_file"] is None


def test_the_capture_is_rendered_at_the_ptys_size():
    """A redraw over the first frame (cursor up, erase line) leaves only the
    newest picker on the screen, the way the agent repaints it."""
    first = "\r\n".join(_screen(PLAN_MODE))
    moved = [line.replace(CUR, " ") for line in PLAN_MODE]
    moved[1] = moved[1].replace("   2.", f" {CUR} 2.")
    # Up to the first option row, then rewrite the four option rows.
    up = len(_screen(PLAN_MODE)) - _screen(PLAN_MODE).index(PLAN_MODE[0]) - 1
    redraw = f"\x1b[{up}A\r" + "".join(f"\x1b[2K{row}\r\n" for row in moved)
    lines = pp.screen_lines(first + redraw, rows=40, cols=60)
    assert pp.parse_picker(lines)["cursor"] == 2


def test_the_capture_tail_undoes_text_mode_newlines(tmp_path: Path):
    """The session-host writes the capture in text mode: ``\\n`` is on disk
    as ``os.linesep``. The reader recovers the agent's bytes exactly, lone
    ``\\r`` included, and never starts mid-line when it cuts."""
    path = tmp_path / "s.transcript"
    agent_bytes = "one\r\ntwo\rTWO\nthree\r\n"
    with path.open("w", encoding="utf-8") as fh:
        fh.write(agent_bytes)
    assert pp.read_capture_tail(path) == agent_bytes
    assert pp.read_capture_tail(path, n_bytes=12).startswith(("TWO", "three"))
    assert pp.read_capture_tail(tmp_path / "missing.transcript") is None


def test_the_plan_file_is_read_by_bare_name_from_claudes_plans(tmp_path: Path, monkeypatch):
    (tmp_path / "plans").mkdir()
    (tmp_path / "plans" / "tidy-blue-otter.md").write_text("# Plan\n\n- step\n", encoding="utf-8")
    (tmp_path / "secret.md").write_text("no", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert pp.read_plan_file("tidy-blue-otter.md") == ("# Plan\n\n- step", False)
    for name in ("../secret.md", "..\\secret.md", "missing.md", None, "plan.txt"):
        assert pp.read_plan_file(name) is None, name


# ------------------------------------------------------------------- keys


def _picker(options=PLAN_MODE) -> Dict[str, Any]:
    return pp.parse_picker(_screen(options))


def test_a_yes_is_its_digit_and_a_send_back_is_digit_text_enter():
    p = _picker()
    assert pp.answer_keys(p, 2, "Yes, manually approve edits") == [("2", False)]
    assert pp.answer_keys(p, 3, "Tell Claude what to change", "  use\nhello  ") == [
        ("3", False), ("use hello", True),
    ]


@pytest.mark.parametrize("picker,option,label,why", [
    (None, 1, "Yes, auto-accept edits", "no picker on screen"),
    ("bypass", 1, "Yes, auto-accept edits", "the label at that digit changed"),
    ("plan", 4, "Yes, auto-accept edits", "no such option"),
    ("focused", 2, "Yes, manually approve edits", "the text field has focus"),
    ("typed", 3, "use the word hello", "a typed-into field"),
])
def test_a_tap_the_screen_no_longer_backs_types_nothing(picker, option, label, why):
    screens = {
        "plan": PLAN_MODE,
        "bypass": BYPASS_NARROW,
        "focused": [PLAN_MODE[0].replace(CUR, " "), PLAN_MODE[1], f" {CUR} 3. Tell Claude what to change", PLAN_MODE[3]],
        "typed": [PLAN_MODE[0], PLAN_MODE[1], "   3. use the word hello", PLAN_MODE[3]],
    }
    p = _picker(screens[picker]) if picker else None
    with pytest.raises(pp.PickerChanged):
        pp.answer_keys(p, option, label, "some feedback")


@pytest.mark.parametrize("option,label,feedback", [
    (True, "Yes, auto-accept edits", None),
    ("1", "Yes, auto-accept edits", None),
    (3, "Tell Claude what to change", "   "),
    (3, "Tell Claude what to change", "x" * 501),
])
def test_a_malformed_tap_is_a_bad_request(option, label, feedback):
    with pytest.raises(ValueError):
        pp.answer_keys(_picker(), option, label, feedback)


# ----------------------------------------------------------------- routes


def _live(kind: str = "pty", agent: str = "claude") -> Dict[str, Any]:
    return {"session_id": "s1", "kind": kind, "agent": agent, "alive": True,
            "rows": 40, "cols": 60, "project_dir": "E:/automation/proj"}


@pytest.fixture
def picker_client(webapp_client, monkeypatch, tmp_path):
    """The webapp with the gate bypassed, one live Claude PTY whose capture
    is ``capture``, a plans folder, and the PTY socket replaced by a
    recorder — nothing here opens a real socket."""
    from app.webapp import middleware
    from app.webapp.routers import session_transcript as router
    from src import audit

    monkeypatch.setattr(middleware, "LOOPBACK_HOSTS",
                        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}))
    client, _, overrides = webapp_client
    session = overrides["session"]
    session.list_sessions.return_value = [_live()]
    capture = tmp_path / "s1.transcript"
    monkeypatch.setattr(audit, "transcript_path", lambda sid: capture)
    (tmp_path / "plans").mkdir()
    (tmp_path / "plans" / "tidy-blue-otter.md").write_text("# The plan\n\n- one\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(router, "_ANSWER_KEY_GAP_S", 0)
    router._PICKER_SEEN.clear()
    router._CONTEXT_SEEN.clear()
    typed: List[Any] = []

    async def fake_pty(port, sid, keys):
        typed.append((sid, keys))

    monkeypatch.setattr(router, "_type_into_pty", fake_pty)

    def show(options=PLAN_MODE, **kw):
        # Written the way the session-host writes it: text mode.
        with capture.open("w", encoding="utf-8") as fh:
            fh.write("\r\n".join(_screen(options, **kw)))

    return client, session, show, typed


def _get(client):
    return client.get("/api/claude-code/sessions/s1/plan-picker")


def _post(client, body: Dict[str, Any]):
    return client.post("/api/claude-code/sessions/s1/plan-answer", json=body)


def test_plan_routes_are_passkey_gated():
    from app.webapp.middleware import _terminal_guard_level
    for tail in ("plan-picker", "plan-answer"):
        assert _terminal_guard_level(f"/api/claude-code/sessions/abc/{tail}") == "passkey"


def test_the_picker_route_serves_the_screens_options_and_the_plan_file(picker_client):
    client, _, show, _ = picker_client
    show()
    body = _get(client).json()
    assert body["showing"] is True and body["answerable"] is True
    assert [o["label"] for o in body["options"]] == [
        "Yes, auto-accept edits", "Yes, manually approve edits", "Tell Claude what to change",
    ]
    assert body["plan"] == "# The plan\n\n- one" and body["plan_source"] == "file"


def test_the_picker_route_falls_back_to_the_screens_plan(picker_client):
    client, _, show, _ = picker_client
    show(below=[])  # no footer, so no plan file named
    body = _get(client).json()
    assert body["plan_source"] == "screen" and body["plan"].startswith("Make a file")


def test_the_picker_route_says_why_there_is_nothing_to_answer(picker_client):
    client, session, show, _ = picker_client
    assert _get(client).json()["reason"] == "no_screen"   # no capture yet
    show(below=[" \u273d Working\u2026"])
    body = _get(client).json()
    assert body["available"] is True and body["showing"] is False and body["reason"] == "not_showing"
    for live, reason in ((_live(kind="remote"), "detached"), (_live(agent="codex"), "unsupported_agent")):
        session.list_sessions.return_value = [live]
        assert _get(client).json()["reason"] == reason
    session.list_sessions.return_value = []
    assert _get(client).json()["reason"] == "session_not_found"


def test_a_tap_types_the_keys_the_screen_still_backs(picker_client):
    client, _, show, typed = picker_client
    show(BYPASS_NARROW)
    label = "Yes, and switch to BYPASS PERMISSIONS (no further prompts) for this session"
    r = _post(client, {"option": 1, "label": label})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "delivered": "unconfirmed", "answer": "approve"}
    r = _post(client, {"option": 3, "label": "Tell Claude what to change", "feedback": "smaller"})
    assert r.status_code == 200 and r.json()["answer"] == "feedback"
    assert typed == [("s1", [("1", False)]), ("s1", [("3", False), ("smaller", True)])]


def test_a_tap_for_a_label_the_screen_no_longer_shows_types_nothing(picker_client):
    """The card said "auto-accept edits"; the terminal now offers BYPASS at
    that digit. Nothing may be typed."""
    client, session, show, typed = picker_client
    show(BYPASS_NARROW)
    r = _post(client, {"option": 1, "label": "Yes, auto-accept edits"})
    assert r.status_code == 409 and "changed" in r.json()["detail"]
    show(below=[" \u273d Working\u2026"])
    r = _post(client, {"option": 2, "label": "Yes, manually approve edits"})
    assert r.status_code == 409 and "can't see the plan picker" in r.json()["detail"]
    session.list_sessions.return_value = [_live(kind="remote")]
    r = _post(client, {"option": 2, "label": "Yes, manually approve edits"})
    assert r.status_code == 409 and "PC console" in r.json()["detail"]
    session.list_sessions.return_value = []
    assert _post(client, {"option": 2, "label": "x"}).status_code == 409
    assert typed == []


def test_a_bad_tap_is_a_400_and_a_failed_write_says_how_far_it_got(picker_client, monkeypatch):
    from app.webapp.routers import session_transcript as router

    client, _, show, typed = picker_client
    show()
    r = _post(client, {"option": 3, "label": "Tell Claude what to change", "feedback": " "})
    assert r.status_code == 400
    assert typed == []

    async def broken(port, sid, keys):
        raise router._PartialAnswer(1, 3, "terminal socket failed: gone")

    monkeypatch.setattr(router, "_type_into_pty", broken)
    r = _post(client, {"option": 3, "label": "Tell Claude what to change", "feedback": "smaller"})
    assert r.status_code == 502
    assert r.json()["detail"] == "Answer only partly sent (1 of 3 keys): check the terminal"


# ------------------------------------------------------ context use (#1223)

# Footers as the fleet statusline paints them (statusline-command.ps1),
# rendered off live captures at 120 and 46 columns: usage first, then the
# model family, then the folder; at 120 columns Claude Code can print its
# own text further along the same row.
FOOTER = "  27%c 10%s 79%w | opus | fleet-config (main)"
FOOTER_WIDE = (
    "  32%c 4%s 78%w | opus | fleet-config (main)"
    "                                                             316677 tokens"
)
# used_percentage is null early in a session and right after /compact: the
# script omits %c and the row starts at the 5-hour figure.
FOOTER_AFTER_COMPACT = "  5%s 10%w | opus | app-launcher (main)"
PROMPT = [RULE, f"{CUR} ", RULE]
MODE_LINE = "  ⏵⏵ bypass permissions on (shift+tab to cycle)"


def _footer_screen(*footers: str) -> List[str]:
    return ["● Done.", "", *PROMPT, *footers, MODE_LINE, "", ""]


def _write_capture(path: Path, lines: List[str]) -> None:
    # Text mode, the way the session-host writes it.
    with path.open("w", encoding="utf-8") as fh:
        fh.write("\r\n".join(lines))


@pytest.mark.parametrize("lines,expected", [
    (_footer_screen(FOOTER), 27),
    (_footer_screen(FOOTER_WIDE), 32),
    (_footer_screen("  6%c 6%s 76%w | Fable 5.1 | project"), 6),
    (_footer_screen("  100%c | opus | proj"), 100),
    (_footer_screen(FOOTER_AFTER_COMPACT), None),
    (_footer_screen("  opus | proj (main)"), None),
    (_footer_screen(), None),
    (["", " Nothing to see", ""], None),
])
def test_context_is_the_statuslines_percent_c(lines, expected):
    assert context_percent(lines) == expected


def test_only_the_bottom_most_statusline_counts():
    """An older footer higher on the screen (or one quoted in a reply) says
    nothing about now: the newest row wins, and one without %c is None
    rather than the stale figure above it."""
    older = ["  71%c 10%s 79%w | opus | proj", ""]
    assert context_percent(older + _footer_screen(FOOTER)) == 27
    assert context_percent(older + _footer_screen(FOOTER_AFTER_COMPACT)) is None


def test_text_that_only_looks_like_a_footer_is_not_one():
    for line in ("  12%cache hit rate", "  5%status", "  context at 40%c", "  1234%c | x"):
        assert context_percent(_footer_screen(line)) is None, line


def test_context_reads_off_the_rendered_capture():
    """Through the real render: the colour escapes the script wraps around
    ``NN%c`` (green/yellow/red) are gone once pyte has drawn the row."""
    raw = "\r\n".join([
        "● Done.", "", *PROMPT,
        "  \x1b[33m64%c\x1b[0m 10%s 79%w | opus | proj (main)", MODE_LINE,
    ])
    assert context_percent(pp.screen_lines(raw, rows=12, cols=60)) == 64


def _get_context(client):
    return client.get("/api/claude-code/sessions/s1/context")


def test_the_context_route_is_passkey_gated():
    from app.webapp.middleware import _terminal_guard_level
    assert _terminal_guard_level("/api/claude-code/sessions/abc/context") == "passkey"


def test_the_context_route_serves_the_footers_percent(picker_client, tmp_path):
    client, _, _, _ = picker_client
    capture = tmp_path / "s1.transcript"
    _write_capture(capture, _footer_screen(FOOTER))
    assert _get_context(client).json() == {"available": True, "percent": 27, "reason": None}
    _write_capture(capture, _footer_screen(FOOTER_AFTER_COMPACT))
    assert _get_context(client).json() == {
        "available": True, "percent": None, "reason": "not_showing",
    }


def test_the_context_route_says_why_there_is_no_number(picker_client):
    client, session, _, _ = picker_client
    assert _get_context(client).json() == {
        "available": False, "percent": None, "reason": "no_screen",
    }
    for live, reason in ((_live(kind="remote"), "detached"), (_live(agent="codex"), "unsupported_agent")):
        session.list_sessions.return_value = [live]
        assert _get_context(client).json() == {
            "available": False, "percent": None, "reason": reason,
        }
    session.list_sessions.return_value = []
    assert _get_context(client).json()["reason"] == "session_not_found"


def test_the_context_route_logs_a_change_not_a_tick(picker_client, tmp_path, caplog):
    client, _, _, _ = picker_client
    capture = tmp_path / "s1.transcript"
    _write_capture(capture, _footer_screen(FOOTER))
    with caplog.at_level("INFO", logger="app.webapp.routers.session_transcript"):
        for _ in range(3):
            _get_context(client)
        _write_capture(capture, _footer_screen(FOOTER_AFTER_COMPACT))
        _get_context(client)
    lines = [r.getMessage() for r in caplog.records if "context s1" in r.getMessage()]
    assert lines == ["ℹ️ context s1: showing (27%)", "ℹ️ context s1: not showing"]
