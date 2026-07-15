"""Manual title override — a launcher-native rename (issue #458).

The one rename channel that works identically for every launcher-supported
agent, including a detached ``RemoteSession`` (no PTY, no OSC, no
first-prompt capture — see the ``RemoteSession`` docstring). Kept in-memory
on the session object, like ``live_title``/``prompt_title``, so it needs no
persistence or cleanup: it dies with the session.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from src.session_host import (
    _MANUAL_TITLE_MAX_CHARS,
    PtySession,
    SessionManager,
)


def _make_pty_session(manager: SessionManager, agent: str = "claude") -> PtySession:
    session = PtySession(
        session_id="sid-test",
        project_dir=r"C:\code\app-launcher",
        name="app-launcher",
        flags="",
        started_at=time.time(),
        _loop=MagicMock(),
        _pty=MagicMock(name="PtyProcess"),
        agent=agent,
    )
    manager._sessions[session.session_id] = session
    return session


class _FakeCompleted:
    def __init__(self, stdout: str = "4321\n") -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


def _make_remote_session(manager: SessionManager, tmp_path, monkeypatch):
    from src import session_host

    monkeypatch.setattr(
        session_host.subprocess, "run", lambda *a, **k: _FakeCompleted()
    )
    return manager.create_remote(str(tmp_path), "proj", "", "codex")


def test_rename_sets_manual_title_on_pty_session():
    mgr = SessionManager()
    session = _make_pty_session(mgr)

    result = mgr.rename(session.session_id, "my custom title")

    assert result is session
    assert session.manual_title == "my custom title"
    assert session.to_api()["manual_title"] == "my custom title"


def test_rename_clears_with_empty_title():
    mgr = SessionManager()
    session = _make_pty_session(mgr)
    mgr.rename(session.session_id, "first title")

    mgr.rename(session.session_id, "")

    assert session.manual_title == ""
    assert session.to_api()["manual_title"] == ""


def test_rename_strips_and_caps_length():
    mgr = SessionManager()
    session = _make_pty_session(mgr)

    mgr.rename(session.session_id, "  " + ("x" * (_MANUAL_TITLE_MAX_CHARS + 20)) + "  ")

    assert len(session.manual_title) == _MANUAL_TITLE_MAX_CHARS
    assert session.manual_title == "x" * _MANUAL_TITLE_MAX_CHARS


def test_rename_unknown_session_returns_none():
    mgr = SessionManager()
    assert mgr.rename("no-such-session", "title") is None


def test_rename_works_on_detached_remote_session(tmp_path, monkeypatch):
    """The one title channel that reaches a RemoteSession — no PTY needed."""
    mgr = SessionManager()
    session = _make_remote_session(mgr, tmp_path, monkeypatch)

    result = mgr.rename(session.session_id, "detached rename")

    assert result is session
    assert session.manual_title == "detached rename"
    assert session.to_api()["manual_title"] == "detached rename"


def test_manual_title_defaults_empty_in_to_api():
    mgr = SessionManager()
    session = _make_pty_session(mgr)
    assert session.to_api()["manual_title"] == ""


# --- native-CLI rename forwarding (issue #503) --------------------------------


def test_rename_forwards_native_command_into_pty():
    """A pty-kind rename also types the agent's native command into the PTY —
    ESC to clear the prompt, then `/rename <title>` + CR (Claude)."""
    mgr = SessionManager()
    session = _make_pty_session(mgr, agent="claude")

    mgr.rename(session.session_id, "my custom title")

    calls = [c.args for c in session._pty.write.call_args_list]
    assert ("\x1b",) in calls
    assert ("/rename my custom title\r",) in calls


def test_rename_uses_pi_name_verb():
    """Pi's built-in rename verb is /name, not /rename (#503)."""
    mgr = SessionManager()
    session = _make_pty_session(mgr, agent="pi")

    mgr.rename(session.session_id, "pi thread")

    calls = [c.args for c in session._pty.write.call_args_list]
    assert ("/name pi thread\r",) in calls
    assert ("/rename pi thread\r",) not in calls


def test_rename_clear_does_not_write_to_pty():
    """Clearing a title (empty string) sets manual_title but injects nothing —
    a title clear has no sensible agent-side semantics."""
    mgr = SessionManager()
    session = _make_pty_session(mgr)

    mgr.rename(session.session_id, "")

    assert session.manual_title == ""
    session._pty.write.assert_not_called()


def test_rename_caps_injected_title_length():
    """The injected command carries the same stripped/capped title as
    manual_title — not the raw over-length input."""
    mgr = SessionManager()
    session = _make_pty_session(mgr)

    mgr.rename(session.session_id, "  " + ("x" * (_MANUAL_TITLE_MAX_CHARS + 20)) + "  ")

    expected = "x" * _MANUAL_TITLE_MAX_CHARS
    calls = [c.args for c in session._pty.write.call_args_list]
    assert (f"/rename {expected}\r",) in calls


def test_rename_remote_session_attempts_no_pty_write(tmp_path, monkeypatch):
    """A detached RemoteSession keeps today's manual_title-only behaviour —
    the native-command injection is PtySession-only (no PTY to type into)."""
    mgr = SessionManager()
    session = _make_remote_session(mgr, tmp_path, monkeypatch)

    mgr.rename(session.session_id, "detached rename")

    assert session.manual_title == "detached rename"
    # The injection path lives only on PtySession — a RemoteSession never
    # exposes it, so the isinstance guard can never mis-route to it.
    assert not hasattr(session, "inject_rename")
