"""First-prompt session title capture + derivation (issue #266).

Only Claude Code emits a genuine per-conversation OSC title; Antigravity and
Copilot emit none, and Codex/Pi emit only the project folder. For those agents
the session-host derives a human title from the first *submitted* prompt seen
on the input stream. These tests pin the derivation helpers and the
capture-on-first-submit path against a fake PTY, so a refactor can't silently
break the title source or re-introduce the empty-first-line trap.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from src.session_host import (
    _PROMPT_TITLE_BUF_MAX,
    _PROMPT_TITLE_MAX_CHARS,
    _PROMPT_TITLE_MAX_WORDS,
    PtySession,
    _cook_input_line,
    _derive_prompt_title,
)


def _make_session() -> PtySession:
    return PtySession(
        session_id="sid-test",
        project_dir=r"C:\code\app-launcher",
        name="app-launcher",
        flags="",
        started_at=time.time(),
        _loop=MagicMock(),
        _pty=MagicMock(name="PtyProcess"),
    )


# ------------------------------------------------------ _derive_prompt_title

def test_derive_collapses_whitespace_and_trims():
    assert _derive_prompt_title("  fix   the\tlogin   bug ") == "fix the login bug"


def test_derive_caps_word_count():
    title = _derive_prompt_title("one two three four five six seven eight")
    assert len(title.split(" ")) == _PROMPT_TITLE_MAX_WORDS
    assert title == "one two three four five six"


def test_derive_caps_char_length():
    title = _derive_prompt_title("supercalifragilisticexpialidocious " * 4)
    assert len(title) <= _PROMPT_TITLE_MAX_CHARS


def test_derive_empty_returns_empty():
    assert _derive_prompt_title("   ") == ""
    assert _derive_prompt_title("\x00\x01\x02") == ""


# --------------------------------------------------------- _cook_input_line

def test_cook_applies_backspace():
    assert _cook_input_line("helo\x7flo") == "hello"   # DEL
    assert _cook_input_line("abc\x08\x08x") == "ax"     # BS x2


def test_cook_strips_arrow_key_escape():
    # ESC[D is left-arrow — a CSI sequence that must not leak into the title.
    assert _cook_input_line("ab\x1b[Dc") == "abc"


def test_cook_strips_bracketed_paste_markers():
    assert _cook_input_line("\x1b[200~pasted text\x1b[201~") == "pasted text"


def test_cook_strips_osc_sequence():
    assert _cook_input_line("a\x1b]0;window title\x07b") == "ab"


# -------------------------------------------------- first-prompt capture path

def test_first_prompt_captured_on_enter():
    s = _make_session()
    s.write("fix the bug")
    assert s.prompt_title == ""            # typed but not yet submitted
    s.write("\r")
    assert s.prompt_title == "fix the bug"


def test_capture_accumulates_across_keystrokes():
    s = _make_session()
    for ch in "hello world":
        s.write(ch)
    s.write("\r")
    assert s.prompt_title == "hello world"


def test_capture_reflects_backspace_editing():
    s = _make_session()
    s.write("helo")
    s.write("\x7f")        # erase the stray 'o'
    s.write("lo\r")
    assert s.prompt_title == "hello"


def test_capture_only_happens_once():
    s = _make_session()
    s.write("first prompt\r")
    assert s.prompt_title == "first prompt"
    s.write("a second, different prompt\r")
    assert s.prompt_title == "first prompt"   # never overwritten


def test_leading_empty_submit_is_skipped():
    """A bare Enter (or whitespace-only line) must not lock in an empty title —
    the first *meaningful* line should win, even in one combined write."""
    s = _make_session()
    s.write("\r")
    assert s.prompt_title == ""
    assert not s._prompt_captured
    s.write("   \r")          # whitespace-only — still skipped
    assert not s._prompt_captured
    s.write("real first prompt\r")
    assert s.prompt_title == "real first prompt"


def test_empty_then_real_in_one_write():
    s = _make_session()
    s.write("\r\nactual prompt\r")
    assert s.prompt_title == "actual prompt"


def test_capture_from_bracketed_paste():
    s = _make_session()
    s.write("\x1b[200~pasted first prompt\x1b[201~\r")
    assert s.prompt_title == "pasted first prompt"


def test_unsubmitted_overlong_input_finalizes_at_cap():
    """A user who types past the buffer cap without ever pressing Enter still
    gets a title rather than an unbounded buffer."""
    s = _make_session()
    s.write("word " * (_PROMPT_TITLE_BUF_MAX // 4))   # > cap, no newline
    assert s._prompt_captured
    assert s.prompt_title.startswith("word word")


def test_prompt_title_exposed_in_to_api():
    s = _make_session()
    s.write("ship the feature\r")
    assert s.to_api()["prompt_title"] == "ship the feature"


def test_no_capture_before_any_input():
    s = _make_session()
    assert s.to_api()["prompt_title"] == ""
