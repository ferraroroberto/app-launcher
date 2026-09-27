"""The /resume picker reader and session list (#1300, src/resume_picker.py).

Screens are the picker as probed on Claude Code 2.1.283 (the probe is on
#1300), at 140 and 52 columns; transcripts are synthetic.
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


@pytest.fixture
def resume_client(picker_client, monkeypatch, tmp_path):
    """picker_client plus a projects folder holding two resumable sessions,
    short waits, and a PTY recorder that repaints the capture the way the
    terminal would: Escape closes the picker, /resume paints ``after``.
    ``_live()`` is a 60-column PTY and the reader renders at the PTY's size,
    so these tests paint the 52-column picker."""
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
    monkeypatch.setattr(router, "_RESUME_ESCAPE_S", 0.3)
    capture = tmp_path / "s1.transcript"
    state = {"after": None, "escape_closes": True, "live_title": ""}

    def paint(lines):
        capture.write_text("\r\n".join(lines), encoding="utf-8")

    async def fake_pty(port, sid, keys):
        typed.append((sid, keys))
        if keys == [(resume_picker.ESCAPE, False)] and state["escape_closes"]:
            paint(_AFTER_ESC)
        elif keys and keys[0][0].startswith("/resume ") and state["after"] is not None:
            paint(state["after"])
            session.list_sessions.return_value = [{**_live(), "live_title": state["live_title"]}]

    monkeypatch.setattr(router, "_type_into_pty", fake_pty)
    return client, session, paint, typed, state


def test_resume_routes_are_passkey_gated_and_scoped_to_sessions():
    from app.webapp.middleware import _terminal_guard_level
    for tail in ("resume-sessions", "resume"):
        assert _terminal_guard_level(f"/api/claude-code/sessions/abc/{tail}") == "passkey"
    assert _terminal_guard_level("/api/jobs/nightly/resume") != "passkey"


def test_the_picker_poll_reports_the_resume_picker_from_the_same_read(resume_client):
    client, _, paint, _, _ = resume_client
    paint(_PICKER_52)
    body = client.get("/api/claude-code/sessions/s1/plan-picker").json()
    assert body["showing"] is False and body["resume_picker"] is True
    paint(_AFTER_ESC)
    assert client.get("/api/claude-code/sessions/s1/plan-picker").json()["resume_picker"] is False


def test_the_list_route_serves_the_projects_sessions_and_the_picker_state(resume_client):
    client, session, paint, _, _ = resume_client
    paint(_PICKER_52)
    body = client.get("/api/claude-code/sessions/s1/resume-sessions").json()
    assert body["available"] is True and body["picker"] is True
    assert [(s["id"], s["title"]) for s in body["sessions"]] == [
        (_IDS[1], "dark theme please"), (_IDS[0], "Fix login redirect"),
    ]
    session.list_sessions.return_value = [_live(kind="remote")]
    body = client.get("/api/claude-code/sessions/s1/resume-sessions").json()
    assert body == {"available": False, "reason": "detached", "picker": False, "sessions": []}


def test_a_pick_from_the_picker_escapes_it_then_types_the_command_and_reads_it_back(resume_client):
    client, _, paint, typed, state = resume_client
    paint(_PICKER_52)
    state["after"] = ["❯ /resume " + _IDS[0], "", "❯ fix login", "● done"]
    state["live_title"] = "✳ Fix login redirect"
    r = client.post("/api/claude-code/sessions/s1/resume", json={"session_id": _IDS[0], "via": "picker"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "outcome": "resumed", "title": "Fix login redirect"}
    assert typed == [("s1", [("\x1b", False)]), ("s1", [(f"/resume {_IDS[0]}", True)])]


def test_the_outcome_says_not_found_or_unconfirmed_as_the_screen_does(resume_client):
    client, _, paint, typed, state = resume_client
    paint(_AFTER_ESC)
    state["after"] = ["❯ /resume " + _IDS[1], "  ⎿  No conversations found to resume."]
    r = client.post("/api/claude-code/sessions/s1/resume", json={"session_id": _IDS[1], "via": "composer"})
    assert r.status_code == 200 and r.json()["outcome"] == "not_found"
    # No Escape from the composer with no picker up: the command alone.
    assert typed == [("s1", [(f"/resume {_IDS[1]}", True)])]
    state["after"] = ["❯ /resume " + _IDS[1], "", "❯ dark theme please"]
    r = client.post("/api/claude-code/sessions/s1/resume", json={"session_id": _IDS[1], "via": "composer"})
    assert r.json()["outcome"] == "unconfirmed"


def test_nothing_is_typed_when_the_screen_or_the_list_no_longer_backs_the_pick(resume_client):
    client, session, paint, typed, _ = resume_client
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


def test_a_picker_that_stays_up_after_escape_gets_no_command(resume_client):
    client, _, paint, typed, state = resume_client
    paint(_PICKER_52)
    state["escape_closes"] = False
    r = client.post("/api/claude-code/sessions/s1/resume", json={"session_id": _IDS[0], "via": "picker"})
    assert r.status_code == 502 and "did not close" in r.json()["detail"]
    assert typed == [("s1", [("\x1b", False)])]
