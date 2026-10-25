"""Typed-but-unsent composer state, ``composer_dirty`` on ``GET /sessions``
(issue #1643).

The byte sequences below are the ones the 2026-10-17 throwaway-ConPTY probe
typed into Claude Code, Codex CLI and Pi (see the
``src/session_host_composer.py`` docstring for what each agent's screen
showed): xterm's own bytes for each key — ``\\x7f`` Backspace, ``\\x15``
Ctrl-U, ``\\x03`` Ctrl-C, ``\\r`` Enter, ``\\x1b`` Esc. Every transition to
``False`` here is one that was seen to empty the agent's composer; anything
the tracker does not model must read ``None``, never ``False``.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src import session_host_composer
from src.agents import SESSION_HOST_AGENTS
from src.session_host import PtySession, RemoteSession
from src.session_host_composer import ComposerTracker, composer_tracker_for

PROVED = ("claude", "codex", "pi")


def _tracker(agent: str) -> ComposerTracker:
    tracker = composer_tracker_for(agent)
    assert tracker is not None
    return tracker


def _dirty(tracker: ComposerTracker, *writes: str):
    for data in writes:
        tracker.feed(data)
    return tracker.state()[0]


def _make_session(agent: str = "claude") -> PtySession:
    return PtySession(
        session_id="sid-composer",
        project_dir=r"C:\code\app-launcher",
        name="app-launcher",
        flags="",
        started_at=time.time(),
        _loop=MagicMock(),
        _pty=MagicMock(name="PtyProcess"),
        agent=agent,
    )


# ------------------------------------------------ the probed transitions

@pytest.mark.parametrize("agent", PROVED)
def test_new_session_starts_clean(agent):
    assert _tracker(agent).state() == (False, None)


@pytest.mark.parametrize("agent", PROVED)
def test_typing_without_enter_is_dirty(agent):
    assert _dirty(_tracker(agent), "probe alpha") is True


@pytest.mark.parametrize("agent", PROVED)
def test_typing_one_key_per_write_is_dirty(agent):
    """xterm sends one onData frame per keystroke."""
    assert _dirty(_tracker(agent), *"probe") is True


@pytest.mark.parametrize("agent", PROVED)
def test_backspacing_everything_typed_is_clean(agent):
    assert _dirty(_tracker(agent), "probe alpha", "\x7f" * 11) is False


@pytest.mark.parametrize("agent", PROVED)
def test_backspacing_part_of_it_stays_dirty(agent):
    assert _dirty(_tracker(agent), "probe alpha", "\x7f" * 5) is True


@pytest.mark.parametrize("agent", PROVED)
def test_ctrl_u_on_a_one_line_draft_is_clean(agent):
    assert _dirty(_tracker(agent), "probe beta", "\x15") is False


@pytest.mark.parametrize("agent", PROVED)
def test_ctrl_c_on_a_draft_is_clean(agent):
    assert _dirty(_tracker(agent), "probe gamma", "\x03") is False


@pytest.mark.parametrize("agent", PROVED)
def test_enter_on_an_empty_composer_stays_clean(agent):
    assert _dirty(_tracker(agent), "\r") is False


@pytest.mark.parametrize("agent", PROVED)
def test_enter_submits_the_draft(agent):
    """Idle or mid-turn alike: Claude queues it, Codex steers, Pi shows
    "Steering:" — the composer is empty in every case."""
    assert _dirty(_tracker(agent), "fix the login bug", "\r") is False


@pytest.mark.parametrize("agent", PROVED)
def test_one_bare_esc_is_unknown(agent):
    """The probe saw one Esc leave the draft, but a second one, or one that
    interrupts a turn, can change the composer: not modelled."""
    assert _dirty(_tracker(agent), "probe delta", "\x1b") is None


@pytest.mark.parametrize("agent", ["claude", "pi"])
def test_backslash_enter_is_a_newline_in_claude_and_pi(agent):
    tracker = _tracker(agent)
    assert _dirty(tracker, "line one\\", "\r") is True
    # The draft is now two lines; Ctrl-U only empties the second.
    assert _dirty(tracker, "line two", "\x15") is True
    assert _dirty(tracker, "\x03") is False


def test_backslash_enter_submits_in_codex():
    assert _dirty(_tracker("codex"), "line one\\", "\r") is False


# ----------------------------------------------- unknown, never False

@pytest.mark.parametrize(
    "key",
    [
        "\x1b[D",   # left arrow
        "\x1b[A",   # up arrow (history recall)
        "\x1bOA",   # up arrow, application cursor mode
        "\x1b[Z",   # Shift-Tab
        "\t",       # Tab (completion)
        "\x17",     # Ctrl-W
        "\x0b",     # Ctrl-K
        "\x19",     # Ctrl-Y (Claude: paste deleted text)
        "\n",       # bare LF
        "\x1b\r",   # Alt-Enter
    ],
)
def test_unmodelled_keys_are_unknown(key):
    tracker = _tracker("claude")
    assert _dirty(tracker, "draft", key) is None
    # …from a clean composer too: history recall or a completion can fill it.
    tracker = _tracker("claude")
    assert _dirty(tracker, key) is None


def test_ctrl_c_without_a_draft_is_unknown():
    """It interrupts a turn instead, which can restore the turn's prompt."""
    assert _dirty(_tracker("claude"), "\x03") is None


def test_the_submit_after_a_ctrl_s_stash_is_unknown():
    """Claude Code puts a stashed draft back after the next submit."""
    tracker = _tracker("claude")
    assert _dirty(tracker, "half a thought", "\x13") is None
    assert _dirty(tracker, "something else", "\r") is None
    # Only that one submit: the next one clears again.
    assert _dirty(tracker, "and another", "\r") is False


@pytest.mark.parametrize("draft", ["@READ", "see @src/age", "/he"])
def test_enter_into_an_open_completion_popup_is_unknown(draft):
    assert _dirty(_tracker("claude"), draft, "\r") is None


def test_a_slash_command_with_arguments_is_a_submit():
    assert _dirty(_tracker("claude"), "/issue-start 1643", "\r") is False


def test_whitespace_only_enter_is_unknown():
    assert _dirty(_tracker("claude"), "   ", "\r") is None


def test_after_unknown_a_one_word_enter_stays_unknown():
    """The word may have begun in text the tracker never saw (an ``@`` or a
    leading ``/``), so whether a popup takes the Enter is unknown."""
    assert _dirty(_tracker("claude"), "x", "\x1b[D", "word", "\r") is None


def test_after_unknown_a_sentence_enter_is_clean():
    assert _dirty(_tracker("claude"), "x", "\x1b[D", "now send it", "\r") is False


def test_after_unknown_backspace_does_not_claim_clean():
    assert _dirty(_tracker("claude"), "x", "\x1b[D", "ab", "\x7f\x7f\x7f") is None


def test_after_unknown_ctrl_u_does_not_claim_clean():
    """Ctrl-U clears only the cursor's line; unseen text may be above it."""
    assert _dirty(_tracker("claude"), "x", "\x1b[A", "more", "\x15") is None


def test_a_draft_past_the_memory_cap_keeps_only_its_tail():
    """The dropped head is unseen text: backspacing the remembered tail away
    does not claim the composer is empty."""
    cap = session_host_composer._TYPED_MAX_CHARS
    tracker = _tracker("claude")
    assert _dirty(tracker, "x" * (cap + 10)) is True
    assert _dirty(tracker, "\x7f" * cap) is None
    assert _dirty(tracker, "and now a sentence", "\r") is False


def test_a_tracker_fault_is_unknown_and_never_raises(monkeypatch):
    tracker = _tracker("claude")

    def _boom(data):
        raise RuntimeError("tracker bug")

    monkeypatch.setattr(tracker, "_feed", _boom)
    tracker.feed("hello")
    assert tracker.state()[0] is None


def test_mark_unknown():
    tracker = _tracker("claude")
    tracker.mark_unknown()
    assert tracker.state()[0] is None


# --------------------------------------------- bytes that are not keys

def test_terminal_replies_do_not_touch_the_state():
    """An attached xterm answers focus, DA, cursor-position and OSC colour
    queries on its own; none of it is typing."""
    tracker = _tracker("claude")
    assert _dirty(tracker, "\x1b[I", "\x1b[O", "\x1b[?1;2c", "\x1b[12;40R",
                  "\x1b]11;rgb:0000/0000/0000\x07") is False
    assert _dirty(tracker, "draft", "\x1b[I") is True


def test_a_bracketed_paste_is_dirty_and_its_newlines_are_literal():
    tracker = _tracker("claude")
    assert _dirty(tracker, "\x1b[200~line one\rline two\x1b[201~") is True
    assert _dirty(tracker, "\r") is False


def test_an_empty_bracketed_paste_changes_nothing():
    assert _dirty(_tracker("claude"), "\x1b[200~\x1b[201~") is False


# ------------------------------------------------------ dirty_since

def test_dirty_since_is_when_the_composer_left_clean(monkeypatch):
    clock = iter([100.0, 200.0, 300.0])
    monkeypatch.setattr(
        session_host_composer, "time", SimpleNamespace(time=lambda: next(clock))
    )
    tracker = _tracker("claude")
    tracker.feed("a")
    assert tracker.state() == (True, 100.0)
    tracker.feed("b")
    assert tracker.state() == (True, 100.0)
    tracker.feed("\x1b[D")
    assert tracker.state() == (None, 100.0)
    tracker.feed("typed\x03")
    assert tracker.state() == (False, None)
    tracker.feed("c")
    assert tracker.state() == (True, 200.0)


# ---------------------------------------------- agents not probed

@pytest.mark.parametrize(
    "agent", sorted(set(SESSION_HOST_AGENTS) - set(PROVED))
)
def test_unprobed_agents_report_unknown(agent):
    assert composer_tracker_for(agent) is None
    session = _make_session(agent)
    session.write("typed text")
    api = session.to_api()
    assert api["composer_dirty"] is None
    assert api["composer_dirty_since"] is None


def test_unknown_agent_id_has_no_tracker():
    assert composer_tracker_for("no-such-agent") is None


def test_exactly_the_probed_agents_are_tracked():
    tracked = {a for a, spec in SESSION_HOST_AGENTS.items() if spec.composer_tracked}
    assert tracked == set(PROVED)


# ----------------------------------------------- PtySession wiring

def test_session_exposes_the_state_on_to_api():
    session = _make_session("claude")
    assert session.to_api()["composer_dirty"] is False
    session.write("half typed")
    api = session.to_api()
    assert api["composer_dirty"] is True
    assert api["composer_dirty_since"] is not None
    session.write("\r")
    assert session.to_api()["composer_dirty"] is False


def test_server_initiated_writes_are_tracked_too():
    """``submit_input`` writes through ``_write_locked``, not ``write()``:
    its payload sitting unsent in the composer is just as dirty."""
    session = _make_session("claude")
    with session._write_lock:
        session._write_locked("\x1b[200~a deferred steer\x1b[201~")
    assert session.to_api()["composer_dirty"] is True
    with session._write_lock:
        session._write_locked("\r")
    assert session.to_api()["composer_dirty"] is False


def test_a_long_paced_write_is_tracked():
    session = _make_session("codex")
    session.write("x" * 2000)
    assert session.to_api()["composer_dirty"] is True


def test_a_write_that_raises_is_unknown():
    session = _make_session("claude")
    session._pty.write.side_effect = OSError("pipe gone")
    assert session.write("hello") is False
    assert session.to_api()["composer_dirty"] is None


def test_a_write_to_an_exited_session_changes_nothing():
    session = _make_session("claude")
    session._exited = True
    assert session.write("hello") is False
    assert session.to_api()["composer_dirty"] is False


def test_an_interrupt_stop_is_unknown():
    session = _make_session("claude")
    session.stop(mode="interrupt")
    assert session.to_api()["composer_dirty"] is None


def test_a_remote_session_is_always_unknown():
    session = RemoteSession(
        session_id="sid-remote", project_dir=r"C:\x", name="x", flags="",
        started_at=time.time(), pid=4242,
    )
    api = session.to_api()
    assert api["composer_dirty"] is None
    assert api["composer_dirty_since"] is None
