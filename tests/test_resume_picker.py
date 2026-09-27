"""The /resume picker reader and session list (#1300, src/resume_picker.py).

Screens are the picker as probed on Claude Code 2.1.283 (the probe is on
#1300): after ``/resume`` at 140 and 52 columns, and as a ``claude --resume``
launch opens it at 51 columns, with the titles swapped for synthetic ones.
Transcripts are synthetic.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from src import board_exchange, resume_picker

_RULE_140 = "\u2500" * 140
_PICKER_140 = [
    " \u259d\u259c\u2588\u2588\u2588\u2588\u2588\u2588\u2580  Opus 5.5 \u00b7 Claude Max",
    "",
    "\u276f /resume",
    "",
    _RULE_140,
    "  Resume session",
    "  \u256d" + "\u2500" * 134 + "\u256e",
    "  \u2502 \u2315 Search\u2026" + " " * 122 + "\u2502",
    "  \u2570" + "\u2500" * 134 + "\u256f",
    "    resume-probe-1300",
    "",
    "  \u276f Ok bravo lemon",
    "    10 seconds ago \u00b7 HEAD \u00b7 280.4KB",
    "",
    "    Ok alpha kiwi",
    "    1 minute ago \u00b7 HEAD \u00b7 280.4KB",
    "",
    "    Ctrl+A to show all projects \u00b7 Ctrl+B to only show current branch \u00b7 Space to preview \u00b7 Ctrl+R to rename \u00b7 Type to search \u00b7 Esc to",
    "    cancel",
    "",
    "",
]

_PICKER_52 = [
    "\u276f /resume",
    "",
    "\u2500" * 52,
    "  Resume session",
    "  \u256d" + "\u2500" * 46 + "\u256e",
    "  \u2502 \u2315 Search\u2026" + " " * 34 + "\u2502",
    "  \u2570" + "\u2500" * 46 + "\u256f",
    "    resume-probe-1300",
    "",
    "  \u276f /exit",
    "    18 seconds ago \u00b7 HEAD \u00b7 3.5KB",
    "",
    "    Ok bravo lemon",
    "    44 seconds ago \u00b7 HEAD \u00b7 280.4KB",
    "",
    "    Ctrl+A to show all projects \u00b7 Ctrl+B to only",
    "    show current branch \u00b7 Space to preview \u00b7",
    "    Ctrl+R to rename \u00b7 Type to search \u00b7 Esc to",
    "    cancel",
]

# A `claude --resume` launch at 51 columns (the #1300 reopen): a counted
# heading, a "folder \u00b7 folder" group row, a row whose metadata wraps, the
# scroll marker on the last row, and the footer wrapped over four rows.
_RULE_51 = "\u2500" * 51
_PICKER_51 = [
    _RULE_51,
    "  Resume session (1 of 50)",
    "  \u256d" + "\u2500" * 45 + "\u256e",
    "  \u2502 \u2315 Search\u2026" + " " * 35 + "\u2502",
    "  \u2570" + "\u2500" * 45 + "\u256f",
    "    my-project \u00b7 my-project",
    "",
    "  \u276f Fix the login redirect",
    "    14 seconds ago \u00b7 main \u00b7 554.6KB",
    "",
    "    Add a dark theme",
    "    25 minutes ago \u00b7 main \u00b7 5.8MB \u00b7",
    "    example/my-project#12",
    "",
    "    /remote-control is active \u00b7 Continue here,\u2026",
    "    2 hours ago \u00b7 main \u00b7 1012 bytes",
    "",
    "  \u2193 /remote-control is active \u00b7 Continue here,\u2026",
    "    Ctrl+A to show all projects \u00b7 Ctrl+B to only",
    "    show current branch \u00b7 Ctrl+W to show all",
    "    worktrees \u00b7 Space to preview \u00b7 Ctrl+R to",
    "    rename \u00b7 Type to search \u00b7 Esc to cancel",
    "",
]

# The same picker with its search box focused (Up from the first row): no
# row is highlighted and the footer says Escape clears the search.
_PICKER_51_SEARCHING = [row.replace("\u276f", " ") for row in _PICKER_51[:17]] + [
    "    Type to Search \u00b7 Enter to select \u00b7 Esc to",
    "    clear",
    "",
]

_AFTER_ESC = [
    "\u276f /resume",
    "  \u23bf  Resume cancelled",
    "",
    _RULE_140,
    "\u276f",
    _RULE_140,
    "  \u23f5\u23f5 auto mode on (shift+tab to cycle)",
]


def test_the_probed_picker_reads_as_showing_at_both_widths():
    assert resume_picker.parse_resume_picker(_PICKER_140) is True
    assert resume_picker.parse_resume_picker(_PICKER_52) is True


def test_the_resume_launch_picker_reads_at_51_columns_with_its_count_and_wrapped_footer():
    """The reopened #1300 case: a `claude --resume` launch's picker carries a
    "(1 of 50)" count in its heading, which an exact-heading match missed."""
    assert resume_picker.parse_resume_picker(_PICKER_51) is True
    assert resume_picker.picker_total(_PICKER_51) == 50
    assert resume_picker.highlighted_title(_PICKER_51) == "Fix the login redirect"
    assert resume_picker.picker_total(_PICKER_52) is None
    assert resume_picker.highlighted_title(_PICKER_52) == "/exit"


def test_the_picker_with_its_search_box_focused_is_still_up_with_no_row_highlighted():
    assert resume_picker.parse_resume_picker(_PICKER_51_SEARCHING) is True
    assert resume_picker.highlighted_title(_PICKER_51_SEARCHING) is None
    assert resume_picker.highlighted_title(_AFTER_ESC) is None


@pytest.mark.parametrize("shown,title,expected", [
    ("Fix the login redirect", "fix  the LOGIN redirect", True),
    ("Fix the login redirect", "Fix the login", False),
    # The screen cuts a title short with an ellipsis at its width.
    ("/remote-control is active \u00b7 Continue here,\u2026",
     "/remote-control is active \u00b7 Continue here, or on your phone", True),
    ("Continue here,\u2026", "Something else", False),
    # The list caps a title at TITLE_CAP; the screen may show more of it.
    ("x" * 130, "x" * 120, True),
    ("\u2026", "anything", False),
])
def test_a_row_matches_a_title_either_side_may_have_cut_short(shown, title, expected):
    assert resume_picker.title_shows(shown, title) is expected


def test_the_cursor_verdict_is_target_only_when_the_row_can_be_nothing_else():
    one = [{"id": _IDS[0], "title": "Fix the login redirect"},
           {"id": _IDS[1], "title": "Add a dark theme"}]
    assert resume_picker.cursor_on(_PICKER_51, one, _IDS[0]) == "target"
    assert resume_picker.cursor_on(_PICKER_51, one, _IDS[1]) == "other"
    assert resume_picker.cursor_on(_PICKER_51_SEARCHING, one, _IDS[0]) == "other"
    assert resume_picker.cursor_on(_AFTER_ESC, one, _IDS[0]) == "closed"
    # A cut-short row two listed titles both fit: never "target".
    cut = [row.replace("Fix the login redirect", "Fix the login\u2026") for row in _PICKER_51]
    two = one + [{"id": _IDS[2], "title": "Fix the login page styles"}]
    assert resume_picker.cursor_on(cut, two, _IDS[0]) == "ambiguous"


def test_a_picker_pick_is_refused_up_front_when_another_session_has_its_title():
    same = [{"id": _IDS[0], "title": "Continue here"}, {"id": _IDS[1], "title": "continue  HERE"},
            {"id": _IDS[2], "title": "Add a dark theme"}]
    with pytest.raises(resume_picker.ResumeRefused, match="same title"):
        resume_picker.steer_check(same, _IDS[0])
    assert resume_picker.steer_check(same, _IDS[2])["title"] == "Add a dark theme"
    with pytest.raises(resume_picker.ResumeRefused, match="not in this project"):
        resume_picker.steer_check(same, _IDS[3])
    with pytest.raises(ValueError):
        resume_picker.steer_check(same, "nope")


@pytest.mark.parametrize("screen", [
    _AFTER_ESC,
    [],
    # The heading scrolled up into history with a prompt under it now.
    _PICKER_140 + ["\u276f /resume abc", "  \u23bf  No conversations found to resume.", _RULE_140, "\u276f"],
    # A heading with no search box right under it: some other text.
    ["  Resume session", "", "  something else", "    Esc to cancel"],
    # The footer is not the last thing on screen.
    _PICKER_52 + ["", "\u276f "],
])
def test_anything_else_reads_as_not_showing(screen):
    assert resume_picker.parse_resume_picker(screen) is False


# --- the session list ------------------------------------------------------

_CWD = "E:\\work\\my-project"


def _write(folder: Path, sid: str, rows: list, mtime: float) -> Path:
    path = folder / f"{sid}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _user(text, entrypoint="cli", **extra):
    return {"type": "user", "entrypoint": entrypoint, "isSidechain": False,
            "cwd": _CWD, "message": {"role": "user", "content": text}, **extra}


@pytest.fixture
def projects(tmp_path, monkeypatch):
    monkeypatch.setattr(board_exchange, "_CLAUDE_PROJECTS_DIR", tmp_path)
    folder = tmp_path / "E--work-my-project"
    folder.mkdir()
    return folder


_IDS = [f"0000000{i}-0000-4000-8000-00000000000{i}" for i in range(1, 8)]


def test_lists_resumable_sessions_newest_first_with_the_picker_titles(projects):
    now = time.time()
    _write(projects, _IDS[0], [{"type": "mode"}, _user("Fix the login redirect please"),
                               {"type": "ai-title", "aiTitle": "Fix login redirect"}], now - 300)
    _write(projects, _IDS[1], [_user("add dark theme"),
                               {"type": "ai-title", "aiTitle": "Dark theme"},
                               {"type": "custom-title", "customTitle": "theme work"}], now - 60)
    _write(projects, _IDS[2], [_user("a first prompt\nover two lines")], now - 120)
    # A slash-command-only session is titled by its command, as the picker shows it.
    _write(projects, _IDS[3], [_user("<local-command-caveat>Caveat: ...</local-command-caveat>"),
                               _user("<command-name>/exit</command-name>\n<command-message>exit</command-message>")],
           now - 30)
    # Not resumable: print mode, no user turn, a subagent's transcript, a non-id name.
    _write(projects, _IDS[4], [_user("from claude -p", entrypoint="sdk-cli")], now - 10)
    _write(projects, _IDS[5], [{"type": "mode"}, {"type": "permission-mode"}], now - 5)
    (projects / _IDS[0]).mkdir()
    _write(projects / _IDS[0], _IDS[6], [_user("subagent")], now)
    _write(projects, "not-a-session", [_user("stray")], now)

    sessions = resume_picker.list_sessions(_CWD)
    assert [s["id"] for s in sessions] == [_IDS[3], _IDS[1], _IDS[2], _IDS[0]]
    assert [s["title"] for s in sessions] == [
        "/exit", "theme work", "a first prompt over two lines", "Fix login redirect",
    ]
    assert set(sessions[0]) == {"id", "title", "updated_at"}
    assert sessions[0]["updated_at"].endswith("+00:00")


def test_a_long_title_is_capped_and_no_transcript_text_leaves(projects):
    _write(projects, _IDS[0], [_user("x" * 500),
                               {"type": "assistant", "message": {"content": "SECRET REPLY"}}], time.time())
    [only] = resume_picker.list_sessions(_CWD)
    assert len(only["title"]) == resume_picker.TITLE_CAP
    assert "SECRET" not in json.dumps(only)


def test_an_unreadable_projects_folder_is_none_and_an_empty_one_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(board_exchange, "_CLAUDE_PROJECTS_DIR", tmp_path / "missing")
    assert resume_picker.list_sessions(_CWD) is None
    monkeypatch.setattr(board_exchange, "_CLAUDE_PROJECTS_DIR", tmp_path)
    assert resume_picker.list_sessions(_CWD) == []


# --- a pick -----------------------------------------------------------------

def test_a_listed_pick_is_the_resume_command_with_enter():
    listed = [{"id": _IDS[0], "title": "t", "updated_at": "x"}]
    assert resume_picker.resume_keys(listed, _IDS[0]) == [(f"/resume {_IDS[0]}", True)]


def test_an_unlisted_or_unreadable_pick_is_refused_and_a_bad_id_is_rejected():
    listed = [{"id": _IDS[0], "title": "t", "updated_at": "x"}]
    with pytest.raises(resume_picker.ResumeRefused):
        resume_picker.resume_keys(listed, _IDS[1])
    with pytest.raises(resume_picker.ResumeRefused):
        resume_picker.resume_keys(None, _IDS[0])
    for bad in (None, 7, "", "abc", f"{_IDS[0]} && rm", f"{_IDS[0]}\r/exit"):
        with pytest.raises(ValueError):
            resume_picker.resume_keys(listed, bad)


def test_the_outcome_is_read_off_the_line_under_the_echoed_command():
    sid = _IDS[0]
    base = ["\u276f hello", "", f"\u276f /resume {sid}"]
    assert resume_picker.resume_outcome(base + ["  \u23bf  No conversations found to resume."], sid) == "not_found"
    assert resume_picker.resume_outcome(base + ["  \u23bf  Resume cancelled"], sid) == "cancelled"
    assert resume_picker.resume_outcome(base + ["", _RULE_140, "\u276f"], sid) is None
    assert resume_picker.resume_outcome(["\u276f"], sid) is None


# --- routes -------------------------------------------------------------------

from tests.test_plan_picker import PLAN_MODE, _live, _screen, picker_client  # noqa: E402,F401

_PROJ_SLUG = "E--automation-proj"  # _live()'s project_dir, as Claude files it


def _picker_screen(rows: list, cursor: int) -> list:
    """The 52-column picker listing ``rows`` (titles, in the picker's own
    order) with row ``cursor`` highlighted; ``-1`` is the search box."""
    lines = ["\u2500" * 52, "  Resume session",
             "  \u256d" + "\u2500" * 46 + "\u256e",
             "  \u2502 \u2315 Search\u2026" + " " * 34 + "\u2502",
             "  \u2570" + "\u2500" * 46 + "\u256f", "    proj", ""]
    for i, title in enumerate(rows):
        lines += [("  \u276f " if i == cursor else "    ") + title,
                  "    1 minute ago \u00b7 HEAD \u00b7 3.5KB", ""]
    if cursor < 0:
        return lines + ["    Type to Search \u00b7 Enter to select \u00b7 Esc to", "    clear"]
    return lines + ["    Ctrl+A to show all projects \u00b7 Ctrl+B to only",
                    "    show current branch \u00b7 Space to preview \u00b7",
                    "    Ctrl+R to rename \u00b7 Type to search \u00b7 Esc to", "    cancel"]


@pytest.fixture
def resume_client(picker_client, monkeypatch, tmp_path):
    """picker_client plus a projects folder holding two resumable sessions,
    short waits, and a PTY recorder that repaints the capture the way the
    terminal would. The picker is a model of the probed one: Down moves the
    highlight one row and wraps from the last to the first, Enter on a row
    resumes it (paints ``after``). It lists ``state["rows"]`` in its own
    order, which is not the card's. With no picker up, ``/resume <id>``
    paints ``after``. ``_live()`` is a 60-column PTY and the reader renders
    at the PTY's size, so these tests paint the 52-column picker."""
    from app.webapp.routers import session_transcript as router

    client, session, show, typed = picker_client
    root = tmp_path / "claude-projects"
    folder = root / _PROJ_SLUG
    folder.mkdir(parents=True)
    monkeypatch.setattr(board_exchange, "_CLAUDE_PROJECTS_DIR", root)
    now = time.time()
    _write(folder, _IDS[0], [_user("fix login"), {"type": "ai-title", "aiTitle": "Fix login redirect"}], now - 60)
    _write(folder, _IDS[1], [_user("dark theme please")], now - 30)
    monkeypatch.setattr(router, "_RESUME_CONFIRM_S", 0.6)
    monkeypatch.setattr(router, "_RESUME_POLL_S", 0.05)
    monkeypatch.setattr(router, "_RESUME_STEP_S", 0.2)
    monkeypatch.setattr(router, "_RESUME_STEP_POLL_S", 0.02)
    capture = tmp_path / "s1.transcript"
    state = {"after": None, "live_title": "", "rows": ["Fix login redirect", "dark theme please"],
             "cursor": None}

    def paint(lines):
        state["cursor"] = None
        capture.write_text("\r\n".join(lines), encoding="utf-8")

    def paint_picker(cursor=0):
        paint(_picker_screen(state["rows"], cursor))
        state["cursor"] = cursor

    def resumed():
        if state["after"] is not None:
            paint(state["after"])
            session.list_sessions.return_value = [{**_live(), "live_title": state["live_title"]}]

    async def fake_pty(port, sid, keys):
        typed.append((sid, keys))
        if state["cursor"] is None:
            if keys and keys[0][0].startswith("/resume "):
                resumed()
        elif keys == [(resume_picker.DOWN, False)]:
            paint_picker((state["cursor"] + 1) % len(state["rows"]))
        elif keys == [(resume_picker.ENTER, False)] and state["cursor"] >= 0:
            resumed()

    monkeypatch.setattr(router, "_type_into_pty", fake_pty)
    return client, session, paint, typed, state, paint_picker


def test_resume_routes_are_passkey_gated_and_scoped_to_sessions():
    from app.webapp.middleware import _terminal_guard_level
    for tail in ("resume-sessions", "resume"):
        assert _terminal_guard_level(f"/api/claude-code/sessions/abc/{tail}") == "passkey"
    assert _terminal_guard_level("/api/jobs/nightly/resume") != "passkey"


def test_the_picker_poll_reports_the_resume_picker_from_the_same_read(resume_client):
    client, _, paint, _, _, _ = resume_client
    paint(_PICKER_52)
    body = client.get("/api/claude-code/sessions/s1/plan-picker").json()
    assert body["showing"] is False and body["resume_picker"] is True
    paint(_AFTER_ESC)
    assert client.get("/api/claude-code/sessions/s1/plan-picker").json()["resume_picker"] is False


def test_the_list_route_serves_the_projects_sessions_and_the_picker_state(resume_client):
    client, session, paint, _, _, _ = resume_client
    paint(_PICKER_52)
    body = client.get("/api/claude-code/sessions/s1/resume-sessions").json()
    assert body["available"] is True and body["picker"] is True
    assert [(s["id"], s["title"]) for s in body["sessions"]] == [
        (_IDS[1], "dark theme please"), (_IDS[0], "Fix login redirect"),
    ]
    session.list_sessions.return_value = [_live(kind="remote")]
    body = client.get("/api/claude-code/sessions/s1/resume-sessions").json()
    assert body == {"available": False, "reason": "detached", "picker": False, "sessions": []}


_DOWN = [(resume_picker.DOWN, False)]
_ENTER = [(resume_picker.ENTER, False)]


def test_a_pick_from_the_picker_steers_to_its_row_then_selects_it_never_escaping(resume_client):
    """A `claude --resume` launch exits on Escape (probed on #1300), so a
    pick made on the picker moves the highlight to the row and presses Enter
    there. The picker's order is not the card's: the row is found by title."""
    client, _, _, typed, state, paint_picker = resume_client
    paint_picker(0)  # "Fix login redirect" highlighted; the pick is the other row
    state["after"] = ["\u276f dark theme please", "\u25cf done"]
    state["live_title"] = "\u2733 dark theme please"
    r = client.post("/api/claude-code/sessions/s1/resume", json={"session_id": _IDS[1], "via": "picker"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "outcome": "resumed", "title": "dark theme please"}
    assert typed == [("s1", _DOWN), ("s1", _ENTER)]


def test_steering_starts_from_the_search_box_and_wraps_past_the_last_row(resume_client):
    client, _, _, typed, state, paint_picker = resume_client
    state["rows"] = ["dark theme please", "Some other session", "Fix login redirect"]
    paint_picker(-1)  # the search box has focus: no row highlighted
    state["after"] = ["\u276f fix login", "\u25cf done"]
    r = client.post("/api/claude-code/sessions/s1/resume", json={"session_id": _IDS[0], "via": "picker"})
    assert r.status_code == 200, r.text
    assert typed == [("s1", _DOWN)] * 3 + [("s1", _ENTER)]
    typed.clear()
    paint_picker(2)  # on the last row, the pick is the first: one Down wraps
    r = client.post("/api/claude-code/sessions/s1/resume", json={"session_id": _IDS[1], "via": "picker"})
    assert r.status_code == 200, r.text
    assert typed == [("s1", _DOWN), ("s1", _ENTER)]


def test_no_enter_goes_in_when_the_row_is_never_found_or_never_certain(resume_client):
    client, _, _, typed, state, paint_picker = resume_client

    def post():
        return client.post("/api/claude-code/sessions/s1/resume",
                           json={"session_id": _IDS[1], "via": "picker"})

    # The picker does not list it (filtered in the terminal, say): one full
    # lap of Downs, then a refusal.
    state["rows"] = ["Fix login redirect", "Unlisted one"]
    paint_picker(0)
    r = post()
    assert r.status_code == 409 and "isn't in the terminal's picker" in r.json()["detail"]
    assert typed and all(keys == _DOWN for _sid, keys in typed)
    # Its row shows too little of the title to tell it from another.
    typed.clear()
    state["rows"] = ["Fix login redirect", "dark\u2026"]
    _write(board_exchange._CLAUDE_PROJECTS_DIR / _PROJ_SLUG, _IDS[2],
           [_user("dark mode for settings")], time.time() - 90)
    paint_picker(0)
    r = post()
    assert r.status_code == 409 and "too little of that title" in r.json()["detail"]
    assert typed and all(keys == _DOWN for _sid, keys in typed)


def test_a_title_shared_with_another_session_is_refused_before_any_key(resume_client):
    client, _, _, typed, _, paint_picker = resume_client
    _write(board_exchange._CLAUDE_PROJECTS_DIR / _PROJ_SLUG, _IDS[2],
           [_user("Dark theme  PLEASE")], time.time() - 90)
    paint_picker(0)
    r = client.post("/api/claude-code/sessions/s1/resume", json={"session_id": _IDS[1], "via": "picker"})
    assert r.status_code == 409 and "same title" in r.json()["detail"]
    assert typed == []


def test_the_outcome_says_not_found_or_unconfirmed_as_the_screen_does(resume_client):
    client, _, paint, typed, state, _ = resume_client
    paint(_AFTER_ESC)
    state["after"] = ["❯ /resume " + _IDS[1], "  ⎿  No conversations found to resume."]
    r = client.post("/api/claude-code/sessions/s1/resume", json={"session_id": _IDS[1], "via": "composer"})
    assert r.status_code == 200 and r.json()["outcome"] == "not_found"
    # From the composer with no picker up: the command alone.
    assert typed == [("s1", [(f"/resume {_IDS[1]}", True)])]
    state["after"] = ["❯ /resume " + _IDS[1], "", "❯ dark theme please"]
    r = client.post("/api/claude-code/sessions/s1/resume", json={"session_id": _IDS[1], "via": "composer"})
    assert r.json()["outcome"] == "unconfirmed"


def test_nothing_is_typed_when_the_screen_or_the_list_no_longer_backs_the_pick(resume_client):
    client, session, paint, typed, _, _ = resume_client
    post = lambda body: client.post("/api/claude-code/sessions/s1/resume", json=body)  # noqa: E731
    paint(_AFTER_ESC)  # the picker closed
    r = post({"session_id": _IDS[0], "via": "picker"})
    assert r.status_code == 409 and "closed" in r.json()["detail"]
    paint(_PICKER_52)
    r = post({"session_id": _IDS[2], "via": "picker"})  # not in this project's list
    assert r.status_code == 409 and "not in this project's list" in r.json()["detail"]
    paint(_screen(PLAN_MODE))  # the plan picker holds the terminal
    r = post({"session_id": _IDS[0], "via": "composer"})
    assert r.status_code == 409 and "plan" in r.json()["detail"]
    for bad in ({"session_id": "nope", "via": "picker"}, {"session_id": _IDS[0], "via": "terminal"}):
        paint(_PICKER_52)
        assert post(bad).status_code == 400
    session.list_sessions.return_value = [_live(kind="remote")]
    r = post({"session_id": _IDS[0], "via": "composer"})
    assert r.status_code == 409 and "PC console" in r.json()["detail"]
    assert typed == []
